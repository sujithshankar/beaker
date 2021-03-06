
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from bkr.server.model import session
from bkr.inttest import data_setup, get_server_base, with_transaction
from bkr.inttest.server.selenium import WebDriverTestCase
from selenium.webdriver.support.ui import WebDriverWait
from bkr.inttest.server.webdriver_utils import login, is_text_present, \
        delete_and_confirm


class TestSystemGroups(WebDriverTestCase):

    def setUp(self):
        with session.begin():
            self.system_owner = data_setup.create_user(password='password')
            self.system = data_setup.create_system(owner=self.system_owner,
                    shared=False)
            self.group = data_setup.create_group()
            self.user_in_group = data_setup.create_user()
            self.user_in_group.groups.append(self.group)
        self.browser = self.get_browser()
        login(self.browser, user=self.system_owner.user_name, password='password')

    def add_group_to_system(self, b, system=None, group=None):
        if not group:
            group = self.group
        if not system:
            system = self.system
        b.get(get_server_base() + 'view/%s' % system.fqdn)
        b.find_element_by_link_text('Groups').click()
        b.find_element_by_name('group.text').send_keys(group.group_name)
        b.find_element_by_xpath("//form[@name='groups']").submit()
        b.find_element_by_xpath('//table[@id="systemgroups"]'
                '//tr/td[1][normalize-space(text())="%s"]' % group.group_name)

    def delete_group_from_system(self, b, system=None, group=None):
        if not system:
            system = self.system
        if not group:
            group = self.group
        b.get(get_server_base() + 'view/%s' % system.fqdn)
        b.find_element_by_link_text('Groups').click()
        delete_and_confirm(b, '//table[@id="systemgroups"]')
        self.assert_(is_text_present(b, '%s Removed' % group.display_name))

    def test_delete_system_group(self):
        b = self.browser
        self.add_group_to_system(b)
        self.delete_group_from_system(b)
        not_the_group = b.find_element_by_xpath('//table[@id="systemgroups"]//tr[position()=last()]/td').text
        self.assert_(not_the_group is not self.group.group_name)

    def test_add_system_group(self):
        b = self.browser
        self.add_group_to_system(b)
        # Make sure it has been persisted
        b.get(get_server_base() + 'view/%s' % self.system.fqdn)
        b.find_element_by_link_text('Groups').click()
        group_just_added = b.find_element_by_xpath('//table[@id="systemgroups"]//tr[position()=last()]/td').text
        self.assert_(group_just_added == self.group.group_name)

    # https://bugzilla.redhat.com/show_bug.cgi?id=917745
    def test_add_group_to_system_twice(self):
        with session.begin():
            test_group = data_setup.create_group()
        b = self.browser
        self.add_group_to_system(b, group=test_group)
        # Make sure it has been persisted
        b.get(get_server_base() + 'view/%s' % self.system.fqdn)
        b.find_element_by_link_text('Groups').click()
        group_just_added = b.find_element_by_xpath('//table[@id="systemgroups"]//tr[position()=last()]/td').text
        self.assert_(group_just_added == test_group.group_name)
        self.add_group_to_system(b, group=test_group)
        self.assertEquals(b.find_element_by_class_name('flash').text,
                          "System '%s' is already in group '%s'" % (self.system.fqdn, test_group.group_name))
