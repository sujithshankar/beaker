
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from bkr.server.model import session, SystemStatus
from bkr.inttest import data_setup
from bkr.inttest.client import run_client, ClientError, ClientTestCase

class ModifySystemTest(ClientTestCase):

    def test_no_changes(self):
        try:
            run_client(['bkr', 'system-modify', 'fqdn.example'])
            self.fail('Must raise')
        except ClientError as e:
            self.assertIn('At least one option is required', e.stderr_output)

    def test_invalid_system(self):
        try:
            run_client(['bkr', 'system-modify', '--owner=somebody',
                        'ireallydontexistblah.test.fqdn'])
            self.fail('Must raise')
        except ClientError as e:
            self.assertIn('System not found', e.stderr_output)

    def test_change_owner(self):
        with session.begin():
            system1 = data_setup.create_system(shared=False)
            system2 = data_setup.create_system(shared=False)
            new_owner = data_setup.create_user()
        run_client(['bkr', 'system-modify', '--owner', new_owner.user_name,
                    system1.fqdn, system2.fqdn])
        with session.begin():
            session.expire_all()
            systems = [system1, system2]
            for system in systems:
                self.assertEquals(system.owner.user_name, new_owner.user_name)
                self.assertEquals(system.activity[-1].field_name, u'Owner')
                self.assertEquals(system.activity[-1].action, u'Changed')
                self.assertEquals(system.activity[-1].new_value,
                                  new_owner.user_name)

        # invalid user
        try:
            run_client(['bkr', 'system-modify', '--owner', 'idontexist',
                        system1.fqdn])
            self.fail('Must raise')
        except ClientError as e:
            self.assertIn('No such user idontexist', e.stderr_output)

        # insufficient permission to change owner
        with session.begin():
            user1 = data_setup.create_user(password='abc')
        try:
            run_client(['bkr', 'system-modify', '--owner', user1.user_name,
                        '--password', 'abc',
                        '--user', user1.user_name,
                        system1.fqdn])
            self.fail('Must raise')
        except ClientError as e:
            self.assertIn('Insufficient permissions: Cannot edit system',
                          e.stderr_output)

    def test_change_condition(self):
        with session.begin():
            system = data_setup.create_system(status=SystemStatus.automated)
        run_client(['bkr', 'system-modify', '--condition=Manual', system.fqdn])
        with session.begin():
            session.expire_all()
            self.assertEquals(system.status, SystemStatus.manual)
            self.assertEquals(system.activity[0].field_name, u'Status')
            self.assertEquals(system.activity[0].new_value, u'Manual')

    def test_invalid_condition(self):
        with session.begin():
            system = data_setup.create_system()
        try:
            run_client(['bkr', 'system-modify',
                    '--condition=Unconditional', system.fqdn])
            self.fail('Must raise')
        except ClientError as e:
            self.assertIn('option --condition: invalid choice', e.stderr_output)
