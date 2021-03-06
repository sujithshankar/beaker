.. _ovirt:
.. _openstack:

Integration with OpenStack
==========================

.. note::

   The OpenStack integration for dynamic system provisioning is classified as
   experimental. It has a number of limitations and may impact the scheduler's 
   performance, therefore it is currently not recommended for use in production 
   on large Beaker instances.

Beaker can optionally be configured to use OpenStack to create
virtual machines on demand for running recipes.

When OpenStack integration is enabled, Beaker will attempt to create a new
virtual machine for each recipe when it is scheduled. If creating the virtual 
machine fails, Beaker will fall back to using the regular hardware
pool for that recipe. Recipes with hardware requirements in
``<hostRequires/>`` which cannot be satisfied by a virtual machine are
excluded from this process.

To enable OpenStack integration, configure the Identity API (Keystone) endpoint
and dashboard (Horizon) URL in :file:`/etc/beaker/server.cfg`::

    # Use OpenStack for running recipes on dynamically created guests.
    openstack.identity_api_url = 'https://openstack.example.com:5000/v2.0'
    openstack.dashboard_url = 'https://openstack.example.com/dashboard/'

Currently Beaker does not support multiple OpenStack regions. Beaker expects 
a single row to exist in the ``openstack_region`` table, referencing the lab 
controller which should be used for OpenStack recipes. You must insert the row 
manually::

    INSERT INTO openstack_region (lab_controller_id)
    SELECT id FROM lab_controller WHERE fqdn = 'lab.example.com';

In order to boot distro installers on OpenStack instances, Beaker relies on 
a special image containing the iPXE network boot loader, which then loads its 
boot configuration from the Beaker server. The 
:program:`beaker-create-ipxe-image` tool creates and uploads a suitable image 
to Glance. You must run this tool once after defining an OpenStack region.

The name for each virtual machine is constructed from the ``guest_name_prefix`` 
setting (see :ref:`admin-configuration`) combined with the recipe ID. If you 
have configured multiple Beaker instances to use the same OpenStack instance, 
make sure you set a distinct value for ``guest_name_prefix`` to avoid name 
collisions.
