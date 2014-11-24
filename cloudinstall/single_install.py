#
# Copyright 2014 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import sys
import json
import time
from cloudinstall.config import Config
from cloudinstall.installbase import ProgressGUI, ProgressTUI
from cloudinstall import utils


log = logging.getLogger('cloudinstall.single_install')


class SingleInstallException(Exception):
    pass


class SingleInstall():

    def __init__(self, opts, display_controller):
        self.opts = opts
        self.config = Config()
        self.display_controller = display_controller
        if self.opts.command == 'headless':
            self.progress = ProgressTUI(self.display_controller, self.config)
        else:
            self.progress = ProgressGUI(self.display_controller, self.config)
        self.container_name = 'uoi-bootstrap'
        self.container_path = '/var/lib/lxc'
        self.container_abspath = os.path.join(self.container_path,
                                              self.container_name)
        self.userdata = os.path.join(
            self.config.cfg_path, 'userdata.yaml')

        # Sets install type
        utils.spew(os.path.join(self.config.cfg_path, 'single'),
                   'auto-generated')

    def prep_userdata(self):
        """ preps userdata file for container install
        """
        render_parts = {'extra_sshkeys': [utils.ssh_readkey()],
                        'extra_pkgs': ['juju-local']}
        if self.opts.extra_ppa:
            render_parts['extra_ppa'] = self.opts.extra_ppa
        dst_file = os.path.join(self.config.cfg_path,
                                'userdata.yaml')
        original_data = utils.load_template('userdata.yaml')
        log.debug("Userdata options: {}".format(render_parts))
        modified_data = original_data.render(render_parts)
        utils.spew(dst_file, modified_data)

    def create_container_and_wait(self):
        """ Creates container and waits for cloud-init to finish
        """
        self.progress.start_task("Creating container")
        utils.container_create(self.container_name, self.userdata)
        utils.container_start(self.container_name)
        utils.container_wait(self.container_name)
        tries = 1
        while not self.cloud_init_finished():
            time.sleep(1)
            tries = tries + 1

    def cloud_init_finished(self):
        """checks cloud-init result.json in container to find out status

        returns True if cloud-init finished with no errors, False if
        it's not done yet, and raises an exception if it had errors.
        """
        cmd = 'sudo cat /run/cloud-init/result.json'
        try:
            result_json = utils.container_run(self.container_name, cmd)
            # convert from bytes for json to process
            result_json = result_json.decode("utf-8")
            log.debug(result_json)
        except:
            log.debug("Waiting for cloud-init status result")
            return False

        if result_json == '':
            return False

        ret = json.loads(result_json)
        errors = ret['v1']['errors']
        if len(errors):
            log.error("Container cloud-init finished with "
                      "errors: {}".format(errors))
            # FIXME: Log errors for now, don't be fatal as the main
            # error is coming from a pollinate command unable
            # to run which doesn't seem to effect the installer.
            # raise Exception("Container cloud-init returned errors")
        return True

    def copy_installdata_and_set_perms(self):
        """ copies install data and sets permissions on files/dirs
        """
        try:
            utils.chown(self.config.cfg_path,
                        utils.install_user(),
                        utils.install_user(),
                        recursive=True)
            utils.get_command_output("sudo chmod 700 {}".format(
                self.config.cfg_path))
            utils.get_command_output("sudo chmod 600 -R {}/*".format(
                self.config.cfg_path))
        except:
            raise SingleInstallException(
                "Unable to set ownership for {}".format(self.config.cfg_path))

        # copy over the rest of our installation data from host
        # and setup permissions

        # setup charm configurations
        charm_conf = utils.load_template('charmconf.yaml')
        charm_conf_modified = charm_conf.render(
            openstack_password=self.config.openstack_password)
        utils.spew(os.path.join(self.config.cfg_path,
                                'charmconf.yaml'),
                   charm_conf_modified)

        utils.container_run(
            self.container_name, 'mkdir -p .cloud-install')
        utils.container_run(
            self.container_name, 'sudo mkdir -p /etc/openstack')

        utils.container_cp(self.container_name,
                           os.path.join(
                               utils.install_home(), '.cloud-install/*'),
                           '.cloud-install/.')

        utils.container_run(
            self.container_name, "chmod 700 .cloud-install")

        utils.container_run(
            self.container_name, "chmod 600 -R .cloud-install/*")

        # our ssh keys too
        utils.container_cp(self.container_name,
                           os.path.join(utils.install_home(),
                                        '.ssh/id_rsa*'),
                           '.ssh/.')
        utils.container_run(self.container_name, "chmod 600 .ssh/id_rsa*")

    def run(self):
        self.progress.register_tasks([
            "Initializing Environment",
            "Creating container",
            "Starting Juju server"])

        self.progress.start_task("Initializing Environment")
        self.do_install()

    @utils.async
    def do_install(self):
        self.display_controller.info_message("Building environment")
        if os.path.exists(self.container_abspath):
            # Container exists, handle return code in installer
            raise Exception("Container exists, please uninstall or kill "
                            "existing cloud before proceeding.")

        utils.ssh_genkey()

        # Prepare cloud-init file for creation
        self.prep_userdata()

        # Start container
        self.create_container_and_wait()

        # configure juju environment for bootstrap
        single_env = utils.load_template('juju-env/single.yaml')
        single_env_modified = single_env.render(
            openstack_password=self.config.openstack_password)
        utils.spew('/tmp/single.yaml', single_env_modified)
        utils.container_run(self.container_name,
                            'mkdir -p .juju')
        utils.container_cp(self.container_name,
                           '/tmp/single.yaml',
                           '.juju/environments.yaml')

        # Set permissions
        self.copy_installdata_and_set_perms()

        # start the party
        cloud_status_bin = ['openstack-status']
        self.display_controller.info_message("Bootstrapping Juju ..")
        self.progress.start_task("Starting Juju server")
        utils.container_run(self.container_name, "juju bootstrap")
        utils.container_run(self.container_name, "juju status")

        if self.opts.install_only:
            log.info("Done installing, stopping here per --install-only.")
            sys.exit(0)

        self.display_controller.info_message("Starting cloud deployment ..")
        utils.container_run_status(
            self.container_name, " ".join(cloud_status_bin))
