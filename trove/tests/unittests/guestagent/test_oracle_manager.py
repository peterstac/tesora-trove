# flake8: noqa
 
# Copyright (c) 2015 Tesora, Inc.
#
# This file is part of the Tesora DBaas Platform Enterprise Edition.
#
# Tesora DBaaS Platform is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License
# for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# According to sec. 7 of the GNU Affero General Public License, version 3, the
# terms of the AGPL are supplemented with the following terms:
#
# "Tesora", "Tesora DBaaS Platform", and the Tesora logo are trademarks
#  of Tesora, Inc.,
#
# The licensing of the Program under the AGPL does not imply a trademark
# license. Therefore any rights, title and interest in our trademarks remain
# entirely with us.
#
# However, if you propagate an unmodified version of the Program you are
# allowed to use the term "Tesora" solely to indicate that you distribute the
# Program. Furthermore you may use our trademarks where it is necessary to
# indicate the intended purpose of a product or service provided you use it in
# accordance with honest practices in industrial or commercial matters.
#
# If you want to propagate modified versions of the Program under the name
# "Tesora" or "Tesora DBaaS Platform", you may only do so if you have a written
# permission by Tesora, Inc. (to acquire a permission please contact
# Tesora, Inc at trademark@tesora.com).
#
# The interactive user interface of the software displays an attribution notice
# containing the term "Tesora" and/or the logo of Tesora.  Interactive user
# interfaces of unmodified and modified versions must display Appropriate Legal
# Notices according to sec. 5 of the GNU Affero General Public License,
# version 3, when you propagate unmodified or modified versions of  the
# Program. In accordance with sec. 7 b) of the GNU Affero General Public
# License, version 3, these Appropriate Legal Notices must retain the logo of
# Tesora or display the words "Initial Development by Tesora" if the display of
# the logo is not reasonably feasible for technical reasons.

import sys
import testtools
import mock

import testtools.content as content
import testtools.matchers as matchers
import trove.common.cfg as cfg
import trove.common.context as context
import trove.common.exception as exception

# cx_Oracle library requires an Oracle installation, but we don't
# need it for the unittests, so just mock it
sys.modules['cx_Oracle'] = mock.Mock()
import trove.guestagent.datastore.oracle_ra.manager as manager
import trove.guestagent.datastore.oracle_ra.service as dbaas


CONF = cfg.CONF

class GuestAgentManagerTest(testtools.TestCase):
    def setUp(self):
        super(GuestAgentManagerTest, self).setUp()
        self.origin_OracleAppStatus = dbaas.OracleAppStatus
        self.context = context.TroveContext()
        self.manager = manager.Manager()

    def tearDown(self):
        super(GuestAgentManagerTest, self).tearDown()
        dbaas.OracleAppStatus = self.origin_OracleAppStatus

    def test_list_users(self):
        dbaas.OracleAdmin.list_users = mock.MagicMock(return_value=['user1'])
        users = self.manager.list_users(self.context)
        self.assertThat(users, matchers.Equals(['user1']))
        dbaas.OracleAdmin.list_users.assert_any_call(None, None, False)

    def test_create_database(self):
        # codepath to create
        with mock.patch.object(dbaas.OracleAdmin, 'create_database'):
            self.manager.create_database(self.context)
            dbaas.OracleAdmin.create_database.assert_any_call()

        # verify names
        data = [{'name': 'valid', 'result': True},
                {'name': 'underscore_allowed', 'result': True},
                {'name': '_underscores_allowed_EXCEPT_at_the_start', 'result': False},
                {'name': 'CAPS_are_OK', 'result': True},
                {'name': '123_numbers_allowed_890', 'result': True},
                {'name': 'dashes-not-allowed', 'result': False},
                {'name': 'this_IS_valid_but_is_the_max_size_1234567890_fyi_max_len_is_64__',
                 'result': True},
                {'name': 'this_IS_NOT_valid_as_it_is_greater_than_the_max_size_of_64_chars_',
                 'result': False}]

        def _test_name(test):
            CONF.guest_name = test['name']
            # this will cause the name to be dumped if the test fails
            self.addDetail('dbname', content.text_content(test['name']))
            if test['result']:
                self.manager.create_database(self.context)
            else:
                self.assertRaises(exception.BadRequest,
                                  self.manager.create_database,
                                  self.context)

        for set in data:
            _test_name(set)