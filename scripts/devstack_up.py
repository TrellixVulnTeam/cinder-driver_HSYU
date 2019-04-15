#!/usr/bin/env python

"""
Requires:

    Python
    ------
    paramiko

    Utilities
    ---------
    nslookup
    ipmitool
    perl
    git
    sed
"""

from __future__ import unicode_literals, print_function, division

import argparse
import shlex
import subprocess
import sys
import time

try:
    import paramiko
except ImportError:
    print("Paramiko required to run script, run 'pip install paramiko'")
    sys.exit(1)


REBOOT_WAIT = 300
DEVSTACK_WAIT = 300
TEMPEST_WAIT = 300
INITIAL_SSH_TIMEOUT = 600
DAT_CINDER_URL = "http://github.com/Datera/cinder-driver"
DAT_GLANCE_URL = "http://github.com/Datera/glance-driver"
DEV_DRIVER_LOC = "/opt/stack/cinder/cinder/volume/drivers/datera/"
DEV_GLANCE_CONF = "/etc/glance/glance-api.conf"

LOCALCONF = r"""
[[local|localrc]]
SERVICE_HOST=127.0.0.1
ACTIVE_TIMEOUT=90
BOOT_TIMEOUT=90
ASSOCIATE_TIMEOUT=60
TERMINATE_TIMEOUT=60
MYSQL_PASSWORD=secrete
DATABASE_PASSWORD=secrete
RABBIT_PASSWORD=secrete
ADMIN_PASSWORD=secrete
SERVICE_PASSWORD=secrete
SERVICE_TOKEN=111222333444
LIBVIRT_TYPE=kvm

# Screen console logs will capture service logs.
SYSLOG=False
LOGDIR=/opt/stack/logs
SCREEN_LOGS=/opt/stack/logs/screen
LOGFILE=/opt/stack/devstacklog.txt
LOG_COLOR=True
VERBOSE=True
VIRT_DRIVER=libvirt
LOG_COLOR=True
CINDER_PERIODIC_INTERVAL=10
CINDER_SECURE_DELETE=False
API_RATE_LIMIT=False
TEMPEST_HTTP_IMAGE=http://127.0.0.1/
USE_SCREEN=True

# Issues with timeouts to openstack.git
# move to https direct to github as
# its been reported as more reliable
#GIT_BASE=https://github.com

# Add these until pbr 1.8 lands in reqs
REQUIREMENTS_MODE=strict
# Set to False to disable the use of upper-constraints.txt
# if you want to experience the wild freedom of uncapped
# dependencies from PyPI
# USE_CONSTRAINTS=True

# Currently skipped in the gate, so lets skip them too
SKIP_EXERCISES=boot_from_volume,bundle,client-env,euca

# Settings to enable use of Datera
CINDER_ENABLED_BACKENDS=datera
TEMPEST_VOLUME_DRIVER=DateraDriver
TEMPEST_VOLUME_VENDOR=Datera
TEMPEST_STORAGE_PROTOCOL=iSCSI

CINDER_BRANCH={patchset}

[[post-config|/etc/cinder/cinder.conf]]
[DEFAULT]
iscsi_target_prefix=iqn:
CINDER_ENABLED_BACKENDS=datera
[datera]
volume_driver=cinder.volume.drivers.datera.datera_iscsi.DateraDriver
san_is_local=True
san_ip={mgmt_ip}
san_login=admin
san_password=password
datera_tenant_id={tenant}
volume_backend_name=datera
datera_debug_replica_count_override=True
"""


class SSH(object):
    def __init__(self, ip, username, password):
        self.ip = ip
        self.username = username
        self.password = password

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(
            paramiko.AutoAddPolicy())
        # Normal username/password usage
        self.ssh.connect(
            hostname=self.ip,
            username=self.username,
            password=self.password,
            banner_timeout=INITIAL_SSH_TIMEOUT)

    def exec_command(self, command, fail_ok=False):
        s = self.ssh
        msg = "Executing command: {} on VM: {}".format(command, self.ip)
        print(msg)
        _, stdout, stderr = s.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        result = None
        if int(exit_status) == 0:
            result = stdout.read()
        elif fail_ok:
            result = stderr.read()
        else:
            raise EnvironmentError(
                "Nonzero return code: {} stderr: {}".format(
                    exit_status,
                    stderr.read()))
        return result


def setup_stack_user(ssh):
    cmd = ""
    try:
        ssh.exec_command("which yum")
        cmd = "yum install git python-setuptools -y"
    except EnvironmentError:
        cmd = "apt-get install git -y"
    ssh.exec_command(cmd)

    cmd = "git clone http://github.com/openstack-dev/devstack"
    ssh.exec_command(cmd)

    cmd = "cd devstack/tools && ./create-stack-user.sh"
    ssh.exec_command(cmd)

    cmd = "passwd stack"
    msg = "Executing command: {} on VM: {}".format(cmd, ssh.ip)
    print(msg)
    stdin, _, _ = ssh.ssh.exec_command(cmd)
    stdin.write('stack\n')
    stdin.write('stack\n')


def install_devstack(ssh, cluster_ip, tenant, patchset, version):
    cmd = "git clone http://github.com/openstack-dev/devstack"
    ssh.exec_command(cmd)
    lcnf = LOCALCONF.format(
        mgmt_ip=cluster_ip,
        tenant=tenant,
        patchset=patchset)
    ssh.exec_command('echo "{}" > devstack/local.conf'.format(lcnf))
    if _install_devstack(ssh, version) == "SetupTools":
        # For some reason this is removed by devstack during initial setup
        # but sticks after we reinstall it and re-stack
        ssh.exec_command("sudo yum install python-setuptools -y")
        _unstack(ssh)
        _install_devstack(ssh, version)


def _unstack(ssh):
    ssh.exec_command("cd devstack && ./unstack.sh >/dev/null 2>&1 &")
    time.sleep(30)


def _install_devstack(ssh, version):
    if version != "master":
        cmd = ("cd devstack && git checkout {} && ./stack.sh >/dev/null "
               "2>&1 &".format(version))
    else:
        cmd = "cd devstack && ./stack.sh >/dev/null 2>&1 &"
    ssh.exec_command(cmd)

    count = 0
    increment = 10
    while count <= DEVSTACK_WAIT:
        try:
            ssh.exec_command(
                ("grep 'This is your host IP address:' "
                 "/opt/stack/devstacklog.txt"))
            break
        except EnvironmentError:
            try:
                # This means python-setuptools needs to be reinstalled again
                ssh.exec_command(
                    ("grep 'operator not allowed in environment markers' "
                     "/opt/stack/devstacklog.txt"))
                return "SetupTools"
            except EnvironmentError:
                pass
            time.sleep(increment)
            count += increment
    if count >= DEVSTACK_WAIT:
        raise EnvironmentError("Timeout expired before stack.sh "
                               "completed")


def _update_drivers(ssh, mgmt_ip, cinder_version, glance_version):
    # Install python sdk to ensure we're using latest version
    ssh.exec_command("sudo pip install -U dfs_sdk")
    # Install cinder driver
    ssh.exec_command("git clone {}".format(DAT_CINDER_URL))
    if cinder_version != "master":
        ssh.exec_command("cd cinder-driver && git checkout {}".format(
            cinder_version))
    ssh.exec_command("cd cinder-driver/src/datera && cp *.py {}".format(
        DEV_DRIVER_LOC))
    ssh.exec_command("sudo service devstack@c-vol restart")

    # Install glance driver
    install, entryp = _find_glance_dirs(ssh)
    if not install or not entryp:
        print("Could not find all glance install directories: [{}, {}]".format(
            install, entryp))
    ssh.exec_command("git clone {}".format(DAT_GLANCE_URL))
    ssh.exec_command("cd glance-driver/src && sudo cp *.py {}".format(
        "/".join((install, "_drivers"))))
    # Modify entry_points.txt file
    cmd = ("sudo sed -i 's/vmware = glance_store._drivers.vmware_datastore:"
           "Store/datera = glance_store._drivers.datera:Store/' {}".format(
               entryp))
    ssh.exec_command(cmd)
    # Modify backend.py file
    backend = "/".join((install, "backend.py"))
    cmd = "sudo sed -i 's/vsphere/datera/' {}".format(backend)
    ssh.exec_command(cmd)
    # Modify glance-api.conf file, this is gross, but the easiest way
    # of doing a straight replace of these strings.  We have to use
    # perl instead of sed because sed really doesn't want to match
    # multiline strings
    glance_store = """
[glance_store]
filesystem_store_datadir = /opt/stack/data/glance/images/
""".replace("/", "\\/").replace('[', '\\[').replace(']', '\\]')
    new_glance_store = """
[glance_store]
filesystem_store_datadir = /opt/stack/data/glance/images/
stores = file,datera
default_store = datera
datera_san_ip = {}
datera_san_login = admin
datera_san_password = password
datera_replica_count = 1
""".format(mgmt_ip).replace("/", "\\/").replace('[', '\\[').replace(']', '\\]')
    try:
        ssh.exec_command("grep datera_san_ip {}".format(DEV_GLANCE_CONF))
    except EnvironmentError:
        cmd = "perl -i -0pe 's/{}/{}/' {}".format(
                glance_store, new_glance_store, DEV_GLANCE_CONF)
        ssh.exec_command(cmd)
    # Modify glance filters
    cmd = "cd glance-driver/etc/glance && sudo cp -r * /etc/glance/"
    ssh.exec_command(cmd)
    ssh.exec_command("sudo service devstack@g-api restart")


def _find_glance_dirs(ssh):
    cmd = "sudo find /usr -name 'glance_store' 2>/dev/null"
    install = ssh.exec_command(cmd).strip()
    cmd = "sudo find /usr -name 'glance_store*dist-info' 2>/dev/null"
    info = ssh.exec_command(cmd).strip()
    return (install, "/".join((info, "entry_points.txt")))


def run_tempest(ssh):
    ssh.exec_command("cd /opt/stack/tempest && "
                     "tox -e all-plugin -- volume "
                     "> console.out.log 2>/dev/null &")
    count = 0
    increment = 10
    while count <= TEMPEST_WAIT:
        try:
            result = ssh.exec_command(
                r"grep -oP '- Failed: \d+' console.out.log")
            return int(result.split(":")[-1])
            break
        except EnvironmentError:
            time.sleep(increment)
            count += increment
    if count >= DEVSTACK_WAIT:
        raise EnvironmentError("Timeout expired before stack.sh "
                               "completed")


def reinstall_node(ssh, ip):
    print("Wiping node")
    cmd = r"nslookup {} | grep -oP '(\w+).tlx.daterainc.com'"
    host = subprocess.check_output(shlex.split(cmd.format(ip)))
    if not host:
        raise ValueError("Couldn't determine hostname from ip: {}".format(ip))
    subprocess.check_output(shlex.split(
        'ipmitool -H {}-ipmi.tlx.daterainc.com -U '
        'root -P carnifex -I lanplus chassis bootdev pxe'.format(host)))
    ssh.exec_command("dd if=/dev/zero of=/dev/sda bs=1M count=500 && reboot")
    # Wait for node to reboot
    time.sleep(10)
    # Poll for node availability
    count = 0
    increment = 10
    while count <= REBOOT_WAIT:
        try:
            ssh.exec_command("uname -a")
            print("Wipe complete, node accessible")
            break
        except Exception as e:
            if count >= REBOOT_WAIT:
                print(e)
                raise EnvironmentError(
                    "Timeout expired before node became reachable")
            time.sleep(increment)
            count += increment


def main(args):

    if args.only_update_drivers:
        ssh = SSH(args.node_ip, 'stack', 'stack')
        _update_drivers(
            ssh, args.cluster_ip, args.cinder_driver_version,
            args.glance_driver_version)
        return 0
    root_ssh = SSH(args.node_ip, args.username, args.password)
    if args.reimage:
        reinstall_node(root_ssh, args.node_ip)
    setup_stack_user(root_ssh)

    ssh = SSH(args.node_ip, 'stack', 'stack')
    install_devstack(ssh, args.cluster_ip, args.tenant, args.patchset,
                     args.devstack_version)
    _update_drivers(
        ssh, args.cluster_ip, args.cinder_driver_version,
        args.glance_driver_verison)
    if args.run_tempest:
        result = run_tempest(ssh)
        if result == 0:
            print('Tempest Passed!!!')
        else:
            print("Tempest Failed :(, Failures: {}".format(result))
    else:
        print('Devstack setup finished without error')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('cluster_ip')
    parser.add_argument('node_ip')
    parser.add_argument('username')
    parser.add_argument('password')
    parser.add_argument('--cinder-driver-version', default='master')
    parser.add_argument('--glance-driver-version', default='master')
    parser.add_argument('--devstack-version', default='master')
    parser.add_argument('--tenant', default='')
    parser.add_argument('--patchset', default='master')
    parser.add_argument('--run-tempest', action='store_true')
    parser.add_argument('--reimage', action='store_true')
    parser.add_argument('--only-update-drivers', action='store_true')
    args = parser.parse_args()
    sys.exit(main(args))
