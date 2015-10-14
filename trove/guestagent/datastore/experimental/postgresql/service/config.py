# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from collections import OrderedDict
import os

from oslo_log import log as logging

from trove.common import cfg
from trove.common.i18n import _
from trove.common.stream_codecs import PropertiesCodec
from trove.guestagent.common.configuration import ConfigurationManager
from trove.guestagent.common.configuration import OneFileOverrideStrategy
from trove.guestagent.common import guestagent_utils
from trove.guestagent.common import operating_system
from trove.guestagent.common.operating_system import FileMode
from trove.guestagent.datastore.experimental.postgresql.service.process import(
    PgSqlProcess)
from trove.guestagent.datastore.experimental.postgresql.service.status import(
    PgSqlAppStatus)
from trove.guestagent.datastore.experimental.postgresql import pgutil

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

BACKUP_CFG_OVERRIDE = 'PgBaseBackupConfig'
DEBUG_MODE_OVERRIDE = 'DebugLevelOverride'


class PgSqlConfig(PgSqlProcess):
    """Mixin that implements the config API.

    This mixin has a dependency on the PgSqlProcess mixin.
    """

    OS = operating_system.get_os()
    CONFIG_BASE = {
        operating_system.DEBIAN: '/etc/postgresql/',
        operating_system.REDHAT: '/var/lib/postgresql/',
        operating_system.SUSE: '/var/lib/pgsql/'}[OS]
    LISTEN_ADDRESSES = ['*']  # Listen on all available IP (v4/v6) interfaces.

    def __init__(self):
        self._configuration_manager = ConfigurationManager(
            self.PGSQL_CONFIG, self.PGSQL_OWNER, self.PGSQL_OWNER,
            PropertiesCodec(
                delimiter='=',
                string_mappings={'on': True, 'off': False, "''": None}),
            requires_root=True,
            override_strategy=OneFileOverrideStrategy(
                self._init_overrides_dir()))

    # TODO(pmalik): To be removed when
    # 'https://review.openstack.org/#/c/218382/' merges.
    def _init_overrides_dir(self):
        """Initialize a directory for configuration overrides.
        """
        revision_dir = guestagent_utils.build_file_path(
            os.path.dirname(self.PGSQL_CONFIG),
            ConfigurationManager.DEFAULT_STRATEGY_OVERRIDES_SUB_DIR)

        if not os.path.exists(revision_dir):
            operating_system.create_directory(
                revision_dir,
                user=self.PGSQL_OWNER, group=self.PGSQL_OWNER,
                force=True, as_root=True)

        return revision_dir

    @property
    def PGSQL_CONFIG(self):
        return self._find_config_file('postgresql.conf')

    @property
    def PGSQL_HBA_CONFIG(self):
        return self._find_config_file('pg_hba.conf')

    @property
    def PGSQL_IDENT_CONFIG(self):
        return self._find_config_file('pg_ident.conf')

    def _find_config_file(self, name_pattern):
        version_base = guestagent_utils.build_file_path(self.CONFIG_BASE,
                                                        self.pg_version[1])
        return sorted(operating_system.list_files_in_directory(
            version_base, recursive=True, pattern=name_pattern,
            as_root=True), key=len)[0]

    def update_overrides(self, context, overrides, remove=False):
        if remove:
            self.configuration_manager.remove_user_override()
        elif overrides:
            self.configuration_manager.apply_user_override(overrides)

    def apply_overrides(self, context, overrides):
        # Send a signal to the server, causing configuration files to be
        # reloaded by all server processes.
        # Active queries or connections to the database will not be
        # interrupted.
        #
        # NOTE: Do not use the 'SET' command as it only affects the current
        # session.
        pgutil.psql("SELECT pg_reload_conf()")

    def reset_configuration(self, context, configuration):
        """Reset the PgSql configuration to the one given.
        """
        config_contents = configuration['config_contents']
        self.configuration_manager.save_configuration(config_contents)

    def start_db_with_conf_changes(self, context, config_contents):
        """Starts the PgSql instance with a new configuration."""
        if PgSqlAppStatus.get().is_running:
            raise RuntimeError(_("The service is still running."))

        self.configuration_manager.save_configuration(config_contents)
        # The configuration template has to be updated with
        # guestagent-controlled settings.
        self.apply_initial_guestagent_configuration()
        self.start_db(context)

    def apply_initial_guestagent_configuration(self):
        """Update guestagent-controlled configuration properties.
        """
        LOG.debug("Applying initial guestagent configuration.")
        file_locations = {
            'data_directory': self._quote(self.PGSQL_DATA_DIR),
            'hba_file': self._quote(self.PGSQL_HBA_CONFIG),
            'ident_file': self._quote(self.PGSQL_IDENT_CONFIG),
            'external_pid_file': self._quote(self.PID_FILE),
            'unix_socket_directories': self._quote(self.UNIX_SOCKET_DIR),
            'listen_addresses': self._quote(','.join(self.LISTEN_ADDRESSES)),
            'port': CONF.postgresql.postgresql_port}
        self.configuration_manager.apply_system_override(file_locations)
        self._apply_access_rules()

    @staticmethod
    def _quote(value):
        return "'%s'" % value

    def _apply_access_rules(self):
        LOG.debug("Applying database access rules.")

        # Connections to all resources are granted.
        #
        # Local access from administrative users is implicitly trusted.
        #
        # Remote access from the Trove's account is always rejected as
        # it is not needed and could be used by malicious users to hijack the
        # instance.
        #
        # Connections from other accounts always require a double-MD5-hashed
        # password.
        #
        # Make the rules readable only by the Postgres service.
        #
        # NOTE: The order of entries is important.
        # The first failure to authenticate stops the lookup.
        # That is why the 'local' connections validate first.
        # The OrderedDict is necessary to guarantee the iteration order.
        access_rules = OrderedDict(
            [('local', [['all', 'postgres,os_admin', None, 'trust'],
                        ['all', 'all', None, 'md5'],
                        ['replication', 'postgres,os_admin', None, 'trust']]),
             ('host', [['all', 'postgres,os_admin', '127.0.0.1/32', 'trust'],
                       ['all', 'postgres,os_admin', '::1/128', 'trust'],
                       ['all', 'postgres,os_admin', 'localhost', 'trust'],
                       ['all', 'os_admin', '0.0.0.0/0', 'reject'],
                       ['all', 'os_admin', '::/0', 'reject'],
                       ['all', 'all', '0.0.0.0/0', 'md5'],
                       ['all', 'all', '::/0', 'md5']])
             ])
        operating_system.write_file(self.PGSQL_HBA_CONFIG, access_rules,
                                    PropertiesCodec(
                                        string_mappings={'\t': None}),
                                    as_root=True)
        operating_system.chown(self.PGSQL_HBA_CONFIG,
                               self.PGSQL_OWNER, self.PGSQL_OWNER,
                               as_root=True)
        operating_system.chmod(self.PGSQL_HBA_CONFIG, FileMode.SET_USR_RO,
                               as_root=True)

    def disable_backups(self):
        """Reverse overrides applied by PgBaseBackup strategy"""
        if not self.configuration_manager.has_system_override(
                BACKUP_CFG_OVERRIDE):
            return
        LOG.info("Removing configuration changes for backups")
        self.configuration_manager.remove_system_override(BACKUP_CFG_OVERRIDE)
        self.remove_wal_archive_dir()
        self.restart(context=None)

    def enable_backups(self):
        """Apply necessary changes to config to enable WAL-based backups
           if we are using the PgBaseBackup strategy
        """
        LOG.info("Checking if we need to apply changes to WAL config")
        if not CONF.postgresql.backup_strategy == 'PgBaseBackup':
            return
        if self.configuration_manager.has_system_override(BACKUP_CFG_OVERRIDE):
            return

        LOG.info("Applying changes to WAL config for use by base backups")
        arch_cmd = "'test ! -f {wal_arch}/%f && cp %p {wal_arch}/%f'".format(
            wal_arch=CONF.postgresql.wal_archive_location
        )
        opts = {
            # FIXME(atomic77) These spaces after the options are needed until
            # DBAAS-949 is fixed
            'wal_level ': 'hot_standby',
            'archive_mode ': 'on',
            'max_wal_senders': 8,
            # 'checkpoint_segments ': 8,
            'wal_keep_segments': 8,
            'wal_log_hints': 'on',
            'archive_command': arch_cmd
        }
        self.configuration_manager.apply_system_override(opts,
                                                         BACKUP_CFG_OVERRIDE)
        # self.enable_debugging(level=1)
        self.restart(None)

    def disable_debugging(self, level=1):
        """Enable debug-level logging in postgres"""
        self.configuration_manager.remove_system_override(DEBUG_MODE_OVERRIDE)

    def enable_debugging(self, level=1):
        """Enable debug-level logging in postgres"""
        opt = {'log_min_messages': 'DEBUG%s' % level}
        self.configuration_manager.apply_system_override(opt,
                                                         DEBUG_MODE_OVERRIDE)
