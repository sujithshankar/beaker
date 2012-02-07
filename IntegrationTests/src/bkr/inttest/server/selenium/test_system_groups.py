
from turbogears.database import session
from bkr.inttest import data_setup, get_server_base
from bkr.inttest.server.selenium import WebDriverTestCase
from selenium.webdriver.support.ui import WebDriverWait
from bkr.inttest.server.webdriver_utils import login, is_text_present


class TestSystemGroups(WebDriverTestCase):

    def setUp(self):
        self.system_owner = data_setup.create_user(password='password')
        self.system = data_setup.create_system(owner=self.system_owner)
        self.group = data_setup.create_group()
        session.flush()
        self.browser = self.get_browser()
        login(self.browser, user=self.system_owner.user_name, password='password')

    def tearDown(self):
        self.browser.quit()

    def add_group_to_system(self, b, system=None, group=None):
        if not group:
            group = self.group
        if not system:
            system = self.system
        b.get(get_server_base() + 'view/%s' % system.fqdn)
        b.find_element_by_link_text('Groups').click()
        b.find_element_by_name('group.text').send_keys(group.group_name)
        b.find_element_by_link_text('Add ( + )').click()
        group_just_added = b.find_element_by_xpath('//form[@name="groups"]//table//tr[position()=last()]/td').text
        self.assert_(group_just_added == group.group_name)

    def delete_group_from_system(self, b, system=None, group=None):
        if not system:
            system = self.system
        if not group:
            group = self.group
        b.find_element_by_link_text('Delete ( - )').click()
        self.assert_(is_text_present(b, '%s Removed' % group.display_name))

    def test_remove_system_group_admin_privs(self):
        b = self.browser
        self.add_group_to_system(b)
        # This is the link that adds the newly added systemgroup as an admin systemgroup
        b.find_element_by_link_text('(Add)').click()
        b.find_element_by_link_text('(Remove)').click()
        # Check that we have the 'No' link under admin status
        WebDriverWait(b, 5).until(lambda driver: 'No' in driver.find_element_by_xpath('//span[@id="non_admin_group_%s"]' % self.group.group_id).text)
        # Check that we have the 'Add' link under admin status
        WebDriverWait(b, 5).until(lambda driver: 'Add' in driver.find_element_by_xpath('//span[@id="non_admin_group_%s"]' % self.group.group_id).text)

        # Check that it has been persisted
        b.get(get_server_base() + 'view/%s' % self.system.fqdn)
        b.find_element_by_link_text('Groups').click()
        admin_rights_text = b.find_element_by_xpath('//span[@id="non_admin_group_%s"]' % self.group.group_id).text
        self.assert_('No' in admin_rights_text)
        self.assert_('Add' in admin_rights_text)

    def test_add_system_group_admin_privs(self):
        b = self.browser
        self.add_group_to_system(b)
        b.find_element_by_link_text('(Add)').click()
        # Check that we have the 'Yes' link under admin status
        WebDriverWait(b, 5).until(lambda driver: 'Yes' in driver.find_element_by_xpath('//span[@id="admin_group_%s"]' % self.group.group_id).text)
        # Check that we have the 'Remove' link under admin status
        WebDriverWait(b, 5).until(lambda driver: 'Remove' in driver.find_element_by_xpath('//span[@id="admin_group_%s"]' % self.group.group_id).text)

        # Double check it has been persisted
        b.get(get_server_base() + 'view/%s' % self.system.fqdn)
        b.find_element_by_link_text('Groups').click()
        admin_rights_text = b.find_element_by_xpath('//span[@id="admin_group_%s"]' % self.group.group_id).text
        self.assert_('Yes' in admin_rights_text)
        self.assert_('Remove' in admin_rights_text)

    def test_delete_system_group(self):
        b = self.browser
        self.add_group_to_system(b)
        self.delete_group_from_system(b)
        not_the_group = b.find_element_by_xpath('//form[@name="groups"]//table//tr[position()=last()]/td').text
        self.assert_(not_the_group is not self.group.group_name)

    def test_add_system_group(self):
        b = self.browser
        self.add_group_to_system(b)
        # Make sure it has been persisted
        b.get(get_server_base() + 'view/%s' % self.system.fqdn)
        b.find_element_by_link_text('Groups').click()
        group_just_added = b.find_element_by_xpath('//form[@name="groups"]//table//tr[position()=last()]/td').text
        self.assert_(group_just_added == self.group.group_name)