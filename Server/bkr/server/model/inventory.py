
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import sys
import logging
from datetime import datetime, timedelta
from hashlib import md5
from itertools import chain
import urllib
import xml.dom.minidom
import lxml.etree
from kid import XML
from markdown import markdown
from sqlalchemy import (Table, Column, ForeignKey, UniqueConstraint, Index,
        Integer, Unicode, UnicodeText, DateTime, String, Boolean, Numeric, Float,
        BigInteger, VARCHAR, TEXT, event)
from sqlalchemy.sql import select, and_, or_, not_, case, func
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import (mapper, relationship, synonym,
        column_property, dynamic_loader, contains_eager, validates,
        object_mapper, synonym)
from sqlalchemy.orm.attributes import NEVER_SET
from sqlalchemy.orm.collections import attribute_mapped_collection
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.ext.associationproxy import association_proxy
from turbogears import url
from turbogears.config import get
from turbogears.database import session
from bkr.server import identity, metrics, mail
from bkr.server.bexceptions import (BX, InsufficientSystemPermissions,
        StaleCommandStatusException, StaleSystemUserException)
from bkr.server.helpers import make_link
from bkr.server.hybrid import hybrid_property, hybrid_method
from bkr.server.installopts import InstallOptions, global_install_options
from bkr.server.util import is_valid_fqdn
from .base import DeclarativeMappedObject
from .types import (SystemType, SystemStatus, ReleaseAction, CommandStatus,
        SystemPermission, TaskStatus)
from .activity import Activity, ActivityMixin
from .identity import User, Group, SystemGroup
from .lab import LabController
from .distrolibrary import (Arch, KernelType, OSMajor, OSVersion, Distro, DistroTree,
        LabControllerDistroTree)

try:
    #pylint: disable=E0611
    from sqlalchemy.sql.expression import true # SQLAlchemy 0.8+
except ImportError:
    from sqlalchemy.sql import text
    def true():
        return text('TRUE')

log = logging.getLogger(__name__)

xmldoc = xml.dom.minidom.Document()

class SystemActivity(Activity):

    __tablename__ = 'system_activity'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, ForeignKey('activity.id'), primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id'), nullable=False)
    object_id = synonym('system_id')
    object = relationship('System', back_populates='activity')
    __mapper_args__ = {'polymorphic_identity': u'system_activity'}

    def object_name(self):
        return "System: %s" % self.object.fqdn


class CommandActivity(Activity):

    __tablename__ = 'command_queue'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, ForeignKey('activity.id'), primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
            onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    object = relationship('System', back_populates='command_queue')
    system = synonym('object')
    status = column_property(
            Column('status', CommandStatus.db_type(), nullable=False, index=True))
    task_id = Column(String(255))
    delay_until = Column(DateTime, default=None)
    quiescent_period = Column(Integer, default=None)
    updated = Column(DateTime, default=datetime.utcnow)
    callback = Column(String(255))
    distro_tree_id = Column(Integer, ForeignKey('distro_tree.id'))
    distro_tree = relationship(DistroTree)
    kernel_options = Column(UnicodeText)
    __mapper_args__ = {'polymorphic_identity': u'command_activity'}

    def __init__(self, user, service, action, status, callback=None, quiescent_period=0):
        Activity.__init__(self, user, service, action, u'Command', u'', u'')
        self.status = status
        self.callback = callback
        self.quiescent_period = quiescent_period

    def __json__(self):
        return {
            'id': self.id,
            'submitted': self.created,
            'user': self.user,
            'service': self.service,
            'action': self.action,
            'message': self.new_value,
            'status': unicode(self.status),
        }

    def object_name(self):
        return "Command: %s %s" % (self.object.fqdn, self.action)

    def change_status(self, new_status):
        current_status = self.status
        if session.connection(CommandActivity).execute(CommandActivity.__table__.update(
                and_(CommandActivity.__table__.c.id == self.id,
                     CommandActivity.status == current_status)),
                status=new_status).rowcount != 1:
            raise StaleCommandStatusException(
                    'Status for command %s updated in another transaction'
                    % self.id)
        self.status = new_status

    def log_to_system_history(self):
        self.system.record_activity(user=self.user, service=self.service,
                action=self.action, field=u'Power', old=u'',
                new=self.new_value and u'%s: %s' % (self.status, self.new_value)
                    or unicode(self.status))

    def abort(self, msg=None):
        log.error('Command %s aborted: %s', self.id, msg)
        self.status = CommandStatus.aborted
        self.new_value = msg
        self.log_to_system_history()

# Switch to event listen since the AttributeExtension module is
# deprecated in SQLAlchemy 0.9
@event.listens_for(CommandActivity.status, 'set', retval=True)
def handle_cmd_callback(instance, value, oldvalue, initiator):
    if instance.callback:
        try:
            modname, _dot, funcname = instance.callback.rpartition(".")
            module = import_module(modname)
            cb = getattr(module, funcname)
            cb(instance, value)
        except Exception, e:
            log.error("command callback failed: %s" % e)
    return value

class Reservation(DeclarativeMappedObject):

    __tablename__ = 'reservation'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id'), nullable=False)
    system = relationship('System', back_populates='reservations')
    user_id = Column(Integer, ForeignKey('tg_user.user_id'), nullable=False)
    start_time = Column(DateTime, index=True, nullable=False,
            default=datetime.utcnow)
    finish_time = Column(DateTime, index=True)
    # type = 'manual' or 'recipe'
    # XXX Use Enum types
    type = Column(Unicode(30), index=True, nullable=False)
    user = relationship(User, back_populates='reservations')

    def __json__(self):
        return {
            'type': self.type,
            'user': self.user,
            'start_time': self.start_time,
            'finish_time': self.finish_time,
            'recipe_id': self.recipe.id if self.recipe else None,
        }

# this only really exists to make reporting efficient
class SystemStatusDuration(DeclarativeMappedObject):

    __tablename__ = 'system_status_duration'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id'), nullable=False)
    system = relationship('System', back_populates='status_durations')
    status = Column(SystemStatus.db_type(), nullable=False)
    start_time = Column(DateTime, index=True, nullable=False,
            default=datetime.utcnow)
    finish_time = Column(DateTime, index=True)

system_device_map = Table('system_device_map', DeclarativeMappedObject.metadata,
    Column('system_id', Integer,
           ForeignKey('system.id', onupdate='CASCADE', ondelete='CASCADE'),
           primary_key=True, index=True),
    Column('device_id', Integer,
           ForeignKey('device.id'),
           primary_key=True, index=True),
    mysql_engine='InnoDB',
)

system_arch_map = Table('system_arch_map', DeclarativeMappedObject.metadata,
    Column('system_id', Integer,
           ForeignKey('system.id', onupdate='CASCADE', ondelete='CASCADE'),
           primary_key=True, index=True),
    Column('arch_id', Integer,
           ForeignKey('arch.id'),
           primary_key=True, index=True),
    mysql_engine='InnoDB',
)

class System(DeclarativeMappedObject, ActivityMixin):

    __tablename__ = 'system'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    fqdn = Column(Unicode(255), nullable=False, unique=True)
    serial = Column(Unicode(1024))
    date_added = Column(DateTime, default=datetime.utcnow, nullable=False)
    date_modified = Column(DateTime)
    date_lastcheckin = Column(DateTime)
    location = Column(String(255))
    vendor = Column(Unicode(255))
    model = Column(Unicode(255))
    lender = Column(Unicode(255))
    owner_id = Column(Integer, ForeignKey('tg_user.user_id'), nullable=False)
    owner = relationship(User, primaryjoin=owner_id == User.user_id)
    user_id = Column(Integer, ForeignKey('tg_user.user_id'))
    user = relationship(User, primaryjoin=user_id == User.user_id)
    type = Column(SystemType.db_type(), nullable=False)
    status = column_property(Column(SystemStatus.db_type(), nullable=False))
    status_reason = Column(Unicode(4000))
    deleted = Column(Boolean, default=False)
    memory = Column(Integer)
    checksum = Column(String(32))
    lab_controller_id = Column(Integer, ForeignKey('lab_controller.id'))
    lab_controller = relationship(LabController, back_populates='systems')
    mac_address = Column(String(18))
    loan_id = Column(Integer, ForeignKey('tg_user.user_id'))
    loaned = relationship(User, primaryjoin=loan_id == User.user_id)
    loan_comment = Column(Unicode(1000))
    release_action = Column(ReleaseAction.db_type(),
        default=ReleaseAction.power_off, nullable=False)
    reprovision_distro_tree_id = Column(Integer, ForeignKey('distro_tree.id'))
    reprovision_distro_tree = relationship(DistroTree)
    hypervisor_id = Column(Integer, ForeignKey('hypervisor.id'))
    hypervisor = relationship('Hypervisor')
    kernel_type_id = Column(Integer, ForeignKey('kernel_type.id'),
           default=select([KernelType.id], limit=1).where(KernelType.kernel_type==u'default').correlate(None),
           nullable=False)
    kernel_type = relationship('KernelType')
    devices = relationship('Device', secondary=system_device_map,
            back_populates='systems')
    disks = relationship('Disk', back_populates='system',
            cascade='all, delete, delete-orphan')
    arch = relationship(Arch, order_by=[Arch.arch], secondary=system_arch_map,
            back_populates='systems')
    labinfo = relationship('LabInfo', uselist=False, back_populates='system',
            cascade='all, delete, delete-orphan')
    cpu = relationship('Cpu', uselist=False, back_populates='system',
            cascade='all, delete, delete-orphan')
    numa = relationship('Numa', uselist=False, back_populates='system',
            cascade='all, delete, delete-orphan')
    power = relationship('Power', uselist=False, back_populates='system',
            cascade='all, delete, delete-orphan')
    excluded_osmajor = relationship('ExcludeOSMajor', back_populates='system',
            cascade='all, delete, delete-orphan')
    excluded_osversion = relationship('ExcludeOSVersion', back_populates='system',
            cascade='all, delete, delete-orphan')
    provisions = relationship('Provision', back_populates='system',
            cascade='all, delete, delete-orphan',
            collection_class=attribute_mapped_collection('arch'))
    group_assocs = relationship(SystemGroup, back_populates='system',
            cascade='all, delete-orphan')
    key_values_int = relationship('Key_Value_Int', back_populates='system',
            cascade='all, delete, delete-orphan')
    key_values_string = relationship('Key_Value_String', back_populates='system',
            cascade='all, delete, delete-orphan')
    activity = relationship(SystemActivity, back_populates='object', cascade='all, delete',
            order_by=[SystemActivity.__table__.c.id.desc()])
    dyn_activity = dynamic_loader(SystemActivity,
            order_by=[SystemActivity.__table__.c.id.desc()])
    command_queue = relationship(CommandActivity, back_populates='object',
            cascade='all, delete, delete-orphan',
            order_by=[CommandActivity.created.desc(), CommandActivity.id.desc()])
    dyn_command_queue = dynamic_loader(CommandActivity,
            order_by=[CommandActivity.created.desc(), CommandActivity.id.desc()])
    _system_ccs = relationship('SystemCc', back_populates='system',
            cascade='all, delete, delete-orphan')
    reservations = relationship(Reservation, back_populates='system',
            order_by=[Reservation.start_time.desc()])
    dyn_reservations = dynamic_loader(Reservation)
    open_reservation = relationship(Reservation, uselist=False, viewonly=True,
            primaryjoin=and_(id == Reservation.system_id, Reservation.finish_time == None))
    status_durations = relationship(SystemStatusDuration, back_populates='system',
            cascade='all, delete, delete-orphan',
            order_by=[SystemStatusDuration.start_time.desc(),
                      SystemStatusDuration.id.desc()])
    dyn_status_durations = dynamic_loader(SystemStatusDuration)
    custom_access_policy = relationship('SystemAccessPolicy', uselist=False,
            back_populates='system')
    notes = relationship('Note', back_populates='system',
            cascade='all, delete, delete-orphan', order_by='Note.created.desc()')
    queued_recipes = relationship('Recipe', back_populates='systems',
            secondary='system_recipe_map')

    activity_type = SystemActivity

    def __init__(self, fqdn=None, status=SystemStatus.broken, contact=None, location=None,
                       model=None, type=SystemType.machine, serial=None, vendor=None,
                       owner=None, lab_controller=None, lender=None,
                       hypervisor=None, loaned=None, memory=None,
                       kernel_type=None, cpu=None):

        # Ensure the fqdn is valid
        self.fqdn = fqdn

        super(System, self).__init__()
        self.status = status
        self.contact = contact
        self.location = location
        self.model = model
        self.type = type
        self.serial = serial
        self.vendor = vendor
        self.owner = owner
        self.lab_controller = lab_controller
        self.lender = lender
        self.hypervisor = hypervisor
        self.loaned = loaned
        self.memory = memory
        self.kernel_type = kernel_type
        self.cpu = cpu

    @validates('fqdn')
    def validate_fqdn(self, key, fqdn):
        if not fqdn:
            raise ValueError('System must have an associated FQDN')
        if not is_valid_fqdn(fqdn):
            raise ValueError('Invalid FQDN for system: %s' % fqdn)

        return fqdn

    @validates('status_reason')
    def validate_status_reason(self, key, value):
        if value is None:
            return value
        max_length = object_mapper(self).columns[key].type.length
        if len(value) > max_length:
            raise ValueError('System condition report is longer than '
                    '%s characters' % max_length)
        return value

    def to_xml(self, clone=False):
        """ Return xml describing this system """
        fields = dict(
                      hostname    = 'fqdn',
                      system_type = 'type',
                     )

        host_requires = xmldoc.createElement('hostRequires')
        xmland = xmldoc.createElement('and')
        for key in fields.keys():
            require = xmldoc.createElement(key)
            require.setAttribute('op', '=')
            value = getattr(self, fields[key], None) or u''
            require.setAttribute('value', unicode(value))
            xmland.appendChild(require)
        host_requires.appendChild(xmland)
        return host_requires

    def __json__(self):
        # Delayed import to avoid circular dependency
        from . import Recipe
        data = {
            'id': self.id,
            'fqdn': self.fqdn,
            'lab_controller_id': None,
            'possible_lab_controllers': [{'id': lc.id, 'fqdn': lc.fqdn}
                    for lc in LabController.query],
            'owner': self.owner,
            'notify_cc': list(self.cc),
            'status': self.status,
            'possible_statuses': list(SystemStatus),
            'status_reason': self.status_reason,
            'type': self.type,
            'possible_types': list(SystemType),
            'arches': self.arch,
            'possible_arches': Arch.query.all(),
            'kernel_type': self.kernel_type,
            'possible_kernel_types': KernelType.query.all(),
            'location': self.location,
            'lender': self.lender,
            'release_action': self.release_action,
            'possible_release_actions': list(ReleaseAction),
            'reprovision_distro_tree': self.reprovision_distro_tree,
            # The actual power settings are not included here because they must 
            # not be exposed to unprivileged users.
            'has_power': bool(self.power) and bool(self.power.power_type),
            'has_console': False, # IMPLEMENTME
            'created_date': self.date_added,
            'hardware_scan_date': self.date_lastcheckin,
            'hypervisor': self.hypervisor,
            'possible_hypervisors': Hypervisor.query.all(),
            'model': self.model,
            'vendor': self.vendor,
            'serial_number': self.serial,
            'mac_address': self.mac_address,
            'memory': self.memory,
            'numa_nodes': None,
            'cpu_model_name': None,
            'disk_space': None,
            'queue_size': None,
        }
        if self.lab_controller:
            data['lab_controller_id'] = self.lab_controller.id
        if identity.current.user and self.can_view_power(identity.current.user):
            if self.power:
                data.update(self.power.__json__())
            else:
                data.update(Power.empty_json())
        if self.numa:
            data.update({
                'numa_nodes': self.numa.nodes,
            })
        if self.cpu:
            data.update({
                'cpu_model_name': self.cpu.model_name,
            })
        if self.disks:
            data['disk_space'] = sum(disk.size for disk in self.disks)
        if self.status == SystemStatus.automated:
            data['queue_size'] = Recipe.query\
                .filter(Recipe.status == TaskStatus.queued)\
                .filter(Recipe.systems.contains(self))\
                .count()
        # XXX replace with actual recipe data
        recipes = self.dyn_recipes.filter(
                Recipe.finish_time >= datetime.utcnow() - timedelta(days=7))
        data['recipes_run_past_week'] = recipes.count()
        data['recipes_aborted_past_week'] = recipes.filter(
                Recipe.status == TaskStatus.aborted).count()
        # XXX replace with actual status duration data?
        data['status_since'] = self.status_durations[0].start_time
        if self.open_reservation:
            data['current_reservation'] = self.open_reservation
        else:
            data['current_reservation'] = None
        data['previous_reservation'] = self.dyn_reservations\
                .filter(Reservation.finish_time != None)\
                .order_by(Reservation.finish_time.desc()).first()
        if self.loaned is not None:
            data['current_loan'] = self.get_loan_details()
        else:
            data['current_loan'] = None
        if self.custom_access_policy:
            data['access_policy'] = self.custom_access_policy
        else:
            data['access_policy'] = SystemAccessPolicy.empty_json()
        # XXX replace with actual access policy data?
        if identity.current.user:
            u = identity.current.user
            data['can_change_fqdn'] = self.can_edit(u)
            data['can_change_owner'] = self.can_change_owner(u)
            data['can_edit_policy'] = self.can_edit_policy(u)
            data['can_change_notify_cc'] = self.can_edit(u)
            data['can_change_status'] = self.can_edit(u)
            data['can_change_type'] = self.can_edit(u)
            data['can_change_hardware'] = self.can_edit(u)
            data['can_change_power'] = self.can_edit(u)
            data['can_view_power'] = self.can_view_power(u)
            data['can_power'] = self.can_power(u)
            data['can_configure_netboot'] = self.can_configure_netboot(u)
            data['can_take'] = self.is_free(u) and self.can_reserve_manually(u)
            data['can_return'] = (self.open_reservation is not None
                    and self.open_reservation.type != 'recipe'
                    and self.can_unreserve(u))
            data['can_borrow'] = (self.loaned is not u and self.can_borrow(u))
            data['can_lend'] = self.can_lend(u)
            data['can_return_loan'] = (self.loaned is not None
                    and self.can_return_loan(u))
            data['can_reserve'] = self.can_reserve(u)
        else:
            data['can_change_fqdn'] = False
            data['can_change_owner'] = False
            data['can_edit_policy'] = False
            data['can_change_notify_cc'] = False
            data['can_change_status'] = False
            data['can_change_type'] = False
            data['can_change_hardware'] = False
            data['can_change_power'] = False
            data['can_view_power'] = False
            data['can_power'] = False
            data['can_configure_netboot'] = False
            data['can_take'] = False
            data['can_return'] = False
            data['can_borrow'] = False
            data['can_lend'] = False
            data['can_return_loan'] = False
            data['can_reserve'] = False
        return data

    @classmethod
    def all(cls, user):
        """
        Returns a query of systems which the given user is allowed to see.
        If user is None, only includes systems which anonymous users are
        allowed to see.
        """
        if user is None:
            clause = cls.visible_to_anonymous
        else:
            clause = cls.visible_to_user(user)
        return cls.query.outerjoin(System.lab_controller)\
                .outerjoin(System.custom_access_policy)\
                .filter(clause)

    @hybrid_method
    def visible_to_user(self, user):
        if user.is_admin() or user.has_permission(u'secret_visible'):
            return True
        return ((self.custom_access_policy and
                 self.custom_access_policy.grants(user, SystemPermission.view)) or
                self.owner == user or
                self.loaned == user or
                self.user == user)

    @visible_to_user.expression
    def visible_to_user(cls, user): #pylint: disable=E0213
        if user.is_admin() or user.has_permission(u'secret_visible'):
            return true()
        return or_(SystemAccessPolicy.grants(user, SystemPermission.view),
                cls.owner == user,
                cls.loaned == user,
                cls.user == user)

    @hybrid_property
    def visible_to_anonymous(self):
        return (self.custom_access_policy and
                self.custom_access_policy.grants_everybody(SystemPermission.view))

    @visible_to_anonymous.expression
    def visible_to_anonymous(cls): #pylint: disable=E0213
        return SystemAccessPolicy.grants_everybody(SystemPermission.view)

    @hybrid_property
    def visible_to_current_user(self):
        if identity.current.anonymous:
            return self.visible_to_anonymous
        else:
            return self.visible_to_user(identity.current.user)

    @hybrid_method
    def compatible_with_distro_tree(self, distro_tree):
        return (distro_tree.arch in self.arch and
                not any(e.osmajor == distro_tree.distro.osversion.osmajor
                    and e.arch == distro_tree.arch
                    for e in self.excluded_osmajor) and
                not any(e.osversion == distro_tree.distro.osversion
                    and e.arch == distro_tree.arch
                    for e in self.excluded_osversion))

    @compatible_with_distro_tree.expression
    def compatible_with_distro_tree(cls, distro_tree): #pylint: disable=E0213
        return and_(cls.arch.contains(distro_tree.arch),
                not_(cls.excluded_osmajor.any(and_(
                    ExcludeOSMajor.osmajor == distro_tree.distro.osversion.osmajor,
                    ExcludeOSMajor.arch == distro_tree.arch))),
                not_(System.excluded_osversion.any(and_(
                    ExcludeOSVersion.osversion == distro_tree.distro.osversion,
                    ExcludeOSVersion.arch == distro_tree.arch))))

    @hybrid_method
    def in_lab_with_distro_tree(self, distro_tree):
        return (self.lab_controller is not None and
                distro_tree.url_in_lab(self.lab_controller) is not None)

    @in_lab_with_distro_tree.expression
    def in_lab_with_distro_tree(self, distro_tree):
        # we assume System.lab_controller was joined, System.all() does that
        return LabController._distro_trees.any(LabControllerDistroTree
                .distro_tree == distro_tree)

    @classmethod
    def scheduler_ordering(cls, user, query):
        # Order by:
        #   System Owner
        #   System group
        #   Single procesor bare metal system
        return query.outerjoin(System.cpu).order_by(
            case([(System.owner==user, 1),
                (and_(System.owner!=user, System.group_assocs != None), 2)],
                else_=3),
                and_(System.hypervisor == None, Cpu.processors == 1))

    @classmethod
    def mine(cls, user):
        """
        A class method that can be used to search for systems that only
        user can see
        """
        return cls.query.filter(or_(System.user==user,
                                    System.loaned==user))

    @classmethod
    def by_fqdn(cls, fqdn, user):
        """
        A class method that can be used to search systems
        based on the fqdn since it is unique.
        """
        return System.all(user).filter(System.fqdn == fqdn).one()

    @classmethod
    def list_by_fqdn(cls, fqdn, user):
        """
        A class method that can be used to search systems
        based on the fqdn since it is unique.
        """
        return System.all(user).filter(System.fqdn.like('%s%%' % fqdn))

    @classmethod
    def by_id(cls, id, user):
        return System.all(user).filter(System.id == id).one()

    def has_manual_reservation(self, user):
        """Does the specified user currently have a manual reservation?"""
        reservation = self.open_reservation
        return (reservation and reservation.type == u'manual' and
                user and self.user == user)

    def unreserve_manually_reserved(self, *args, **kw):
        open_reservation = self.open_reservation
        if not open_reservation:
            raise BX(_(u'System %s is not currently reserved' % self.fqdn))
        reservation_type = open_reservation.type
        if reservation_type == 'recipe':
            recipe_id = open_reservation.recipe.id
            raise BX(_(u'Currently running R:%s' % recipe_id))
        self.unreserve(reservation=open_reservation, *args, **kw)
        return open_reservation

    def excluded_families(self):
        """
        massage excluded_osmajor for Checkbox values
        """
        major = {}
        version = {}
        for arch in self.arch:
            major[arch.arch] = [osmajor.osmajor.id for osmajor in self.excluded_osmajor_byarch(arch)]
            version[arch.arch] = [osversion.osversion.id for osversion in self.excluded_osversion_byarch(arch)]

        return (major,version)
    excluded_families=property(excluded_families)

    def install_options(self, distro_tree):
        """
        Yields install options based on distro selected.
        Inherit options from Arch -> Family -> Update
        """
        if distro_tree.arch in self.provisions:
            pa = self.provisions[distro_tree.arch]
            yield InstallOptions.from_strings(pa.ks_meta, pa.kernel_options,
                    pa.kernel_options_post)
            if distro_tree.distro.osversion.osmajor in pa.provision_families:
                pf = pa.provision_families[distro_tree.distro.osversion.osmajor]
                yield InstallOptions.from_strings(pf.ks_meta,
                        pf.kernel_options, pf.kernel_options_post)
                if distro_tree.distro.osversion in pf.provision_family_updates:
                    pfu = pf.provision_family_updates[distro_tree.distro.osversion]
                    yield InstallOptions.from_strings(pfu.ks_meta,
                            pfu.kernel_options, pfu.kernel_options_post)

    def manual_provision_install_options(self, distro_tree):
        """
        Manual as in, not a recipe.
        """
        return InstallOptions.reduce(chain(
                [global_install_options()],
                distro_tree.install_options(),
                self.install_options(distro_tree)))

    @property
    def has_efi(self):
        """
        Only relevant for x86 systems. Returns True if the system has EFI 
        firmware, False if the system has BIOS-compatible firmware (or EFI 
        firmware running in BIOS-compatible mode, which is effectively the same 
        thing from software's point of view).

        When no information is available we return False by default, since 
        BIOS-based systems are currently much more common.
        """
        # Currently we just examine NETBOOT_METHOD which is a hack,
        # this bug is about doing something better:
        # https://bugzilla.redhat.com/show_bug.cgi?id=1112439
        return any(kv.key.key_name == u'NETBOOT_METHOD' and kv.key_value == u'efigrub'
                for kv in self.key_values_string)

    @hybrid_method
    def is_free(self, user):
        self._ensure_user_is_authenticated(user)
        return (self.user is None and
                (self.loaned is None or self.loaned == user) and
                (self.lab_controller is None or not self.lab_controller.disabled))

    @is_free.expression
    def is_free(cls, user): #pylint: disable=E0213
        cls._ensure_user_is_authenticated(user)
        return and_(cls.user == None,
                or_(cls.loaned == None, cls.loaned == user),
                or_(LabController.disabled == None, LabController.disabled == False))

    @staticmethod
    def _ensure_user_is_authenticated(user):
        if user is None:
            raise RuntimeError("Cannot check permissions for an "
                               "unauthenticated user.")

    def can_change_owner(self, user):
        """
        Does the given user have permission to change the owner of this system?
        """
        # At least for now, any user that can edit the access policy can
        # also change the system owner (this matches the powers previously
        # granted to "admin" groups for a system)
        return self.can_edit_policy(user)

    def can_edit_policy(self, user):
        """
        Does the given user have permission to edit this system's access policy?
        """
        self._ensure_user_is_authenticated(user)
        if self.owner == user:
            return True
        if user.is_admin():
            return True
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.edit_policy)):
            return True
        return False

    @hybrid_method
    def can_edit(self, user):
        """
        Does the given user have permission to edit details (inventory info, 
        power config, etc) of this system?
        """
        self._ensure_user_is_authenticated(user)
        if self.owner == user:
            return True
        if user.is_admin():
            return True
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.edit_system)):
            return True
        return False

    @can_edit.expression
    def can_edit(cls, user): #pylint: disable=E0213
        cls._ensure_user_is_authenticated(user)
        if user.is_admin():
            return true()
        return or_(SystemAccessPolicy.grants(user, SystemPermission.edit_system),
                cls.owner == user)

    @hybrid_method
    def can_view_power(self, user):
        """
        Does the given user have permission to view power settings for this 
        system?
        """
        self._ensure_user_is_authenticated(user)
        if self.owner == user:
            return True
        if user.is_admin():
            return True
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.edit_system) or
            self.custom_access_policy.grants(user, SystemPermission.view_power)):
            return True
        return False

    @can_view_power.expression
    def can_view_power(cls, user): #pylint: disable=E0213
        cls._ensure_user_is_authenticated(user)
        if user.is_admin():
            return true()
        return or_(SystemAccessPolicy.grants(user, SystemPermission.edit_system),
                SystemAccessPolicy.grants(user, SystemPermission.view_power),
                cls.owner == user)

    def can_lend(self, user):
        """
        Does the given user have permission to loan this system to another user?
        """
        self._ensure_user_is_authenticated(user)
        # System owner is always a loan admin
        if self.owner == user:
            return True
        # Beaker instance admins are loan admins for every system
        if user.is_admin():
            return True
        # Anyone else needs the "loan_any" permission
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.loan_any)):
            return True
        return False

    def can_borrow(self, user):
        """
        Does the given user have permission to loan this system to themselves?
        """
        self._ensure_user_is_authenticated(user)
        # Loan admins can always loan to themselves
        if self.can_lend(user):
            return True
        # "loan_self" only lets you take an unloaned system and update the
        # details on a loan already granted to you
        if ((not self.loaned or self.loaned == user) and
                self.custom_access_policy and
                self.custom_access_policy.grants(user,
                                                 SystemPermission.loan_self)):
            return True
        return False

    def can_return_loan(self, user):
        """
        Does the given user have permission to cancel the current loan for this 
        system?
        """
        self._ensure_user_is_authenticated(user)
        # Users can always return their own loans
        if self.loaned and self.loaned == user:
            return True
        # Loan admins can return anyone's loan
        return self.can_lend(user)

    @hybrid_method
    def can_reserve(self, user):
        """
        Does the given user have permission to reserve this system?

        Note that if is_free() returns False, the user may still not be able
        to reserve it *right now*.
        """
        self._ensure_user_is_authenticated(user)
        # System owner can always reserve the system
        if self.owner == user:
            return True
        # Loans grant the ability to reserve the system
        if self.loaned and self.loaned == user:
            return True
        # Anyone else needs the "reserve" permission
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.reserve)):
            return True
        # Beaker admins can effectively reserve any system, but need to
        # grant themselves the appropriate permissions first (or loan the
        # system to themselves)
        return False

    @can_reserve.expression
    def can_reserve(cls, user): #pylint: disable=E0213
        cls._ensure_user_is_authenticated(user)
        return or_(SystemAccessPolicy.grants(user, SystemPermission.reserve),
                cls.owner == user,
                cls.loaned == user)

    def can_reserve_manually(self, user):
        """
        Does the given user have permission to manually reserve this system?
        """
        self._ensure_user_is_authenticated(user)
        # Manual reservations are permitted only for systems that are
        # either not automated or are currently loaned to this user
        if (self.status != SystemStatus.automated or
              (self.loaned and self.loaned == user)):
            return self.can_reserve(user)
        return False

    def can_unreserve(self, user):
        """
        Does the given user have permission to return the current reservation 
        on this system?
        """
        self._ensure_user_is_authenticated(user)
        # Users can always return their own reservations
        if self.user and self.user == user:
            return True
        # Loan admins can return anyone's reservation
        return self.can_lend(user)

    def can_power(self, user):
        """
        Does the given user have permission to run power/netboot commands on 
        this system?
        The 'configure_netboot' command is treated specially, check the 
        can_configure_netboot method for that instead.
        """
        self._ensure_user_is_authenticated(user)
        if self.can_configure_netboot(user):
            return True
        # Anyone else needs the "control_system" permission
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.control_system)):
            return True
        return False

    def can_configure_netboot(self, user):
        """
        Does the given user have permission to configure netboot (i.e. install 
        a new operating system) for this system?
        We treat this separately from other power commands because it's even 
        more destructive (will typically wipe all disks).
        """
        self._ensure_user_is_authenticated(user)
        # Current user can always control the system
        if self.user and self.user == user:
            return True
        # System owner can always control the system
        if self.owner == user:
            return True
        # Beaker admins can control any system
        if user.is_admin():
            return True
        return False

    def get_loan_details(self):
        """Returns details of the loan as a dict"""
        if not self.loaned:
            return {}
        return {
            'recipient': self.loaned.user_name, # for compat only
            'recipient_user': self.loaned,
            'comment': self.loan_comment,
        }

    def grant_loan(self, recipient, comment, service):
        """Grants a loan to the designated user if permitted"""
        self.change_loan(recipient, comment, service)

    def return_loan(self, service):
        """Grants a loan to the designated user if permitted"""
        self.change_loan(None, None, service)

    def change_loan(self, user_name, comment=None, service='WEBUI'):
        """Changes the current system loan

        change_loan() updates the user a system is loaned to, by
        either adding a new loanee, changing the existing to another,
        or by removing the existing loanee. It also changes the comment
        associated with the loan.

        It checks all permissions that are needed and
        updates SystemActivity.

        Returns the name of the user now holding the loan (if any), otherwise
        returns the empty string.
        """
        loaning_to = user_name
        if loaning_to:
            user = User.by_user_name(loaning_to)
            if not user:
                # This is an error condition
                raise ValueError('user name %s is invalid' % loaning_to)
            if user == identity.current.user:
                if not self.can_borrow(identity.current.user):
                    msg = '%s cannot borrow this system' % user
                    raise InsufficientSystemPermissions(msg)
            else:
                if not self.can_lend(identity.current.user):
                    msg = ('%s cannot lend this system to %s' %
                                           (identity.current.user, user))
                    raise InsufficientSystemPermissions(msg)
        else:
            if not self.can_return_loan(identity.current.user):
                msg = '%s cannot return system loan' % identity.current.user
                raise InsufficientSystemPermissions(msg)
            user = None
            comment = None

        if user != self.loaned:
            self.record_activity(user=identity.current.user, service=service,
                    action=u'Changed', field=u'Loaned To',
                    old=u'%s' % self.loaned if self.loaned else '',
                    new=u'%s' % user if user else '')
            self.loaned = user

        if self.loan_comment != comment:
            self.record_activity(user=identity.current.user, service=service,
                    action=u'Changed', field=u'Loan Comment',
                    old=u'%s' % self.loan_comment if self.loan_comment else '',
                    new=u'%s' % comment if comment else '')
            self.loan_comment = comment

        return loaning_to if loaning_to else ''

    ALLOWED_ATTRS = ['vendor', 'model', 'memory'] #: attributes which the inventory scripts may set
    PRESERVED_ATTRS = ['vendor', 'model'] #: attributes which should only be set when empty

    def get_update_method(self,obj_str):
        methods = dict ( Cpu = self.updateCpu, Arch = self.updateArch,
                         Devices = self.updateDevices, Numa = self.updateNuma,
                         Hypervisor = self.updateHypervisor, Disk = self.updateDisk)
        return methods[obj_str]

    def update_legacy(self, inventory):
        """
        Update Key/Value pairs for legacy RHTS
        """
        keys_to_update = set()
        new_int_kvs = set()
        new_string_kvs = set()
        for key_name, values in inventory.items():
            try:
                key = Key.by_name(key_name)
            except InvalidRequestError:
                continue
            keys_to_update.add(key)
            if not isinstance(values, list):
                values = [values]
            for value in values:
                if isinstance(value, bool):
                    # MySQL will int-ify these, so we do it here 
                    # to make our comparisons accurate
                    value = int(value)
                if key.numeric:
                    new_int_kvs.add((key, int(value)))
                else:
                    new_string_kvs.add((key, unicode(value)))

        # Examine existing key-values to find what we already have, and what 
        # needs to be removed
        for kv in list(self.key_values_int):
            if kv.key in keys_to_update:
                if (kv.key, kv.key_value) in new_int_kvs:
                    new_int_kvs.remove((kv.key, kv.key_value))
                else:
                    self.key_values_int.remove(kv)
                    self.record_activity(user=identity.current.user,
                            service=u'XMLRPC', action=u'Removed', field=u'Key/Value',
                            old=u'%s/%s' % (kv.key.key_name, kv.key_value),
                            new=None)
        for kv in list(self.key_values_string):
            if kv.key in keys_to_update:
                if (kv.key, kv.key_value) in new_string_kvs:
                    new_string_kvs.remove((kv.key, kv.key_value))
                else:
                    self.key_values_string.remove(kv)
                    self.record_activity(user=identity.current.user,
                            service=u'XMLRPC', action=u'Removed', field=u'Key/Value',
                            old=u'%s/%s' % (kv.key.key_name, kv.key_value),
                            new=None)

        # Now we can just add the new ones
        for key, value in new_int_kvs:
            self.key_values_int.append(Key_Value_Int(key, value))
            self.record_activity(user=identity.current.user,
                    service=u'XMLRPC', action=u'Added',
                    field=u'Key/Value', old=None,
                    new=u'%s/%s' % (key.key_name, value))
        for key, value in new_string_kvs:
            self.key_values_string.append(Key_Value_String(key, value))
            self.record_activity(user=identity.current.user,
                    service=u'XMLRPC', action=u'Added',
                    field=u'Key/Value', old=None,
                    new=u'%s/%s' % (key.key_name, value))

        self.date_modified = datetime.utcnow()
        return 0


    def update(self, inventory):
        """ Update Inventory """

        # Update last checkin even if we don't change anything.
        self.date_lastcheckin = datetime.utcnow()

        md5sum = md5("%s" % inventory).hexdigest()
        if self.checksum == md5sum:
            return 0
        self.record_activity(user=identity.current.user,
                service=u'XMLRPC', action=u'Changed', field=u'checksum',
                old=self.checksum, new=md5sum)
        self.checksum = md5sum
        for key in inventory:
            if key in self.ALLOWED_ATTRS:
                if key in self.PRESERVED_ATTRS and getattr(self, key, None):
                    continue
                setattr(self, key, inventory[key])
                self.record_activity(user=identity.current.user,
                        service=u'XMLRPC', action=u'Changed',
                        field=key, old=None, new=inventory[key])
            else:
                try:
                    method = self.get_update_method(key)
                except KeyError:
                    log.warning('Attempted to update unknown inventory property \'%s\' on %s' %
                                (key, self.fqdn))
                else:
                    method(inventory[key])
        self.date_modified = datetime.utcnow()
        return 0

    def updateHypervisor(self, hypervisor):
        if hypervisor:
            hvisor = Hypervisor.by_name(hypervisor)
        else:
            hvisor = None
        if self.hypervisor != hvisor:
            self.record_activity(user=identity.current.user,
                    service=u'XMLRPC', action=u'Changed',
                    field=u'Hypervisor', old=self.hypervisor, new=hvisor)
            self.hypervisor = hvisor

    def updateArch(self, archinfo):
        for arch in archinfo:
            new_arch = Arch.lazy_create(arch=arch)
            if new_arch not in self.arch:
                self.arch.append(new_arch)
                self.record_activity(user=identity.current.user,
                        service=u'XMLRPC', action=u'Added',
                        field=u'Arch', old=None, new=new_arch.arch)

    def updateDisk(self, diskinfo):
        currentDisks = []
        self.disks = getattr(self, 'disks', [])

        for disk in diskinfo['Disks']:
            disk = Disk(**disk)
            if disk not in self.disks:
                self.disks.append(disk)
                self.record_activity(user=identity.current.user,
                        service=u'XMLRPC', action=u'Added',
                        field=u'Disk', old=None, new=disk.size)
            currentDisks.append(disk)

        for disk in self.disks:
            if disk not in currentDisks:
                self.disks.remove(disk)
                self.record_activity(user=identity.current.user,
                        service=u'XMLRPC', action=u'Removed',
                        field=u'Disk', old=disk.size, new=None)

    def updateDevices(self, deviceinfo):
        currentDevices = []
        for device in deviceinfo:
            device_class = DeviceClass.lazy_create(device_class=device['type'])
            mydevice = Device.lazy_create(vendor_id = device['vendorID'],
                                   device_id = device['deviceID'],
                                   subsys_vendor_id = device['subsysVendorID'],
                                   subsys_device_id = device['subsysDeviceID'],
                                   bus = device['bus'],
                                   driver = device['driver'],
                                   device_class_id = device_class.id,
                                   description = device['description'])
            if mydevice not in self.devices:
                self.devices.append(mydevice)
                self.record_activity(user=identity.current.user,
                        service=u'XMLRPC', action=u'Added',
                        field=u'Device', old=None, new=mydevice.id)
            currentDevices.append(mydevice)
        # Remove any old entries
        for device in self.devices[:]:
            if device not in currentDevices:
                self.devices.remove(device)
                self.record_activity(user=identity.current.user,
                        service=u'XMLRPC', action=u'Removed',
                        field=u'Device', old=device.id, new=None)

    def updateCpu(self, cpuinfo):
        # Remove all old CPU data
        if self.cpu:
            for flag in self.cpu.flags:
                session.delete(flag)
            session.delete(self.cpu)

        # Create new Cpu
        cpu = Cpu(vendor     = cpuinfo['vendor'],
                  model      = cpuinfo['model'],
                  model_name = cpuinfo['modelName'],
                  family     = cpuinfo['family'],
                  stepping   = cpuinfo['stepping'],
                  speed      = cpuinfo['speed'],
                  processors = cpuinfo['processors'],
                  cores      = cpuinfo['cores'],
                  sockets    = cpuinfo['sockets'],
                  flags      = cpuinfo['CpuFlags'])

        self.cpu = cpu
        self.record_activity(user=identity.current.user,
                service=u'XMLRPC', action=u'Changed',
                field=u'CPU', old=None,
                new=None) # XXX find a good way to record the actual changes

    def updateNuma(self, numainfo):
        if self.numa:
            session.delete(self.numa)
        if numainfo.get('nodes', None) is not None:
            self.numa = Numa(nodes=numainfo['nodes'])
        self.record_activity(user=identity.current.user,
                service=u'XMLRPC', action=u'Changed',
                field=u'NUMA', old=None,
                new=None) # XXX find a good way to record the actual changes

    def excluded_osmajor_byarch(self, arch):
        """
        List excluded osmajor for system by arch
        """
        excluded = ExcludeOSMajor.query.join('system').\
                    join('arch').filter(and_(System.id==self.id,
                                             Arch.id==arch.id))
        return excluded

    def excluded_osversion_byarch(self, arch):
        """
        List excluded osversion for system by arch
        """
        excluded = ExcludeOSVersion.query.join('system').\
                    join('arch').filter(and_(System.id==self.id,
                                             Arch.id==arch.id))
        return excluded

    def distros(self, query=None):
        """
        List of distros that support this system
        """
        if not query:
            query = Distro.query
        # Tempting to use exists() here but it will not work due to the inner 
        # query also joining to distro, so we cannot correlate... instead we 
        # must join against the distro_tree subquery
        trees_subquery = self.distro_trees()\
                .with_entities(Distro.id.distinct().label('inner_distro_id'))\
                .subquery('inner_distro_trees')
        query = query.join((trees_subquery, trees_subquery.c.inner_distro_id == Distro.id))
        return query

    def distro_trees(self, only_in_lab=True, query=None):
        """
        List of distro trees that support this system
        """
        if not query:
            query = DistroTree.query
        query = query.join(DistroTree.distro, Distro.osversion, OSVersion.osmajor)\
                .options(contains_eager(DistroTree.distro, Distro.osversion, OSVersion.osmajor))
        if only_in_lab:
            query = query.filter(DistroTree.lab_controller_assocs.any(
                    LabControllerDistroTree.lab_controller == self.lab_controller))
        else:
            query = query.filter(DistroTree.lab_controller_assocs.any())
        query = query.filter(DistroTree.arch_id.in_([a.id for a in self.arch]))\
                .filter(not_(OSMajor.excluded_osmajors.any(and_(
                    ExcludeOSMajor.system == self,
                    ExcludeOSMajor.arch_id == DistroTree.arch_id))
                    .correlate(DistroTree.__table__)))\
                .filter(not_(OSVersion.excluded_osversions.any(and_(
                    ExcludeOSVersion.system == self,
                    ExcludeOSVersion.arch_id == DistroTree.arch_id))
                    .correlate(DistroTree.__table__)))
        return query

    def action_release(self, service=u'Scheduler'):
        # Attempt to remove Netboot entry and turn off machine
        self.clear_netboot(service=service)
        if self.release_action == ReleaseAction.power_off:
            self.action_power(action=u'off', service=service)
        elif self.release_action == ReleaseAction.leave_on:
            self.action_power(action=u'on', service=service)
        elif self.release_action == ReleaseAction.reprovision:
            if self.reprovision_distro_tree:
                # There are plenty of things that can go wrong here if the 
                # system or distro tree is misconfigured. But we don't want 
                # that to prevent the recipe from being stopped, so we log 
                # and ignore any errors.
                try:
                    from bkr.server.kickstart import generate_kickstart
                    install_options = self.manual_provision_install_options(
                            self.reprovision_distro_tree)
                    if 'ks' not in install_options.kernel_options:
                        rendered_kickstart = generate_kickstart(install_options,
                            distro_tree=self.reprovision_distro_tree,
                            system=self, user=self.owner)
                        install_options.kernel_options['ks'] = rendered_kickstart.link
                    self.configure_netboot(self.reprovision_distro_tree,
                        install_options.kernel_options_str,
                        service=service)
                    self.action_power(action=u'reboot', service=service)
                except Exception:
                    log.exception('Failed to re-provision %s on %s, ignoring',
                        self.reprovision_distro_tree, self)
        else:
            raise ValueError('Not a valid ReleaseAction: %r' % self.release_action)


    def configure_netboot(self, distro_tree, kernel_options, service=u'Scheduler',
            callback=None):
        if not self.lab_controller:
            return
        self.enqueue_command(u'clear_logs', service=service, callback=callback)
        command = self.enqueue_command(u'configure_netboot',
                service=service, callback=callback)
        command.distro_tree = distro_tree
        command.kernel_options = kernel_options
        return command

    def action_power(self, action=u'reboot', service=u'Scheduler',
            callback=None, delay=0):
        if not self.lab_controller or not self.power:
            return
        if action == u'reboot':
            self.enqueue_command(u'off', service=service,
                    callback=callback, delay=delay,
                    quiescent_period=self.power.power_quiescent_period)
            return self.enqueue_command(u'on', service=service,
                    callback=callback, delay=delay,
                    quiescent_period=self.power.power_quiescent_period)
        else:
            return self.enqueue_command(action, service=service,
                    callback=callback, delay=delay,
                    quiescent_period=self.power.power_quiescent_period)

    def clear_netboot(self, service=u'Scheduler'):
        if not self.lab_controller:
            return
        return self.enqueue_command(u'clear_netboot', service=service)

    def enqueue_command(self, action, service, callback=None,
            quiescent_period=None, delay=None):
        try:
            user = identity.current.user
        except Exception:
            user = None
        activity = CommandActivity(user=user, service=service,
                action=action, status=CommandStatus.queued, callback=callback)
        if quiescent_period:
            activity.quiescent_period = quiescent_period
        if delay:
            activity.delay_until = datetime.utcnow() + timedelta(seconds=delay)
        self.command_queue.insert(0, activity)
        return activity

    def __repr__(self):
        return self.fqdn

    @property
    def href(self):
        """Returns a relative URL for this system's page."""
        return urllib.quote((u'/view/%s' % self.fqdn).encode('utf8'))

    def link(self):
        """ Return a link to this system
        """
        return make_link(url = '/view/%s' % self.fqdn,
                         text = self.fqdn)

    link = property(link)

    def report_problem_href(self, **kwargs):
        return url('/report_problem', system_id=self.id, **kwargs)

    def mark_broken(self, reason, recipe=None, service=u'Scheduler'):
        """Sets the system status to Broken and notifies its owner."""
        try:
            user = identity.current.user
        except Exception:
            user = None
        log.warning('Marking system %s as broken' % self.fqdn)
        self.record_activity(user=user, service=service, action=u'Changed',
                field=u'Status', old=unicode(self.status), new=u'Broken')
        self.status = SystemStatus.broken
        self.date_modified = datetime.utcnow()
        mail.broken_system_notify(self, reason, recipe)

    def suspicious_abort(self):
        # Delayed import to avoid circular dependency
        from . import Recipe
        if self.status == SystemStatus.broken:
            return # nothing to do
        if self.type != SystemType.machine:
            return # prototypes get more leeway, and virtual machines can't really "break"...
        reliable_distro_tag = get('beaker.reliable_distro_tag', None)
        if not reliable_distro_tag:
            return
        # Since its last status change, has this system had an 
        # uninterrupted run of aborted recipes leading up to this one, with 
        # at least two different STABLE distros?
        # XXX this query is stupidly big, I need to do something about it
        session.flush()
        status_change_subquery = session.query(func.max(SystemActivity.created))\
            .filter(and_(
                SystemActivity.system_id == self.id,
                SystemActivity.field_name == u'Status',
                SystemActivity.action == u'Changed'))\
            .subquery()
        nonaborted_recipe_subquery = self.dyn_recipes\
            .filter(Recipe.status != TaskStatus.aborted)\
            .with_entities(func.max(Recipe.finish_time))\
            .subquery()
        count = self.dyn_recipes.join(Recipe.distro_tree, DistroTree.distro)\
            .filter(and_(
                Distro.tags.contains(reliable_distro_tag.decode('utf8')),
                Recipe.start_time >
                    func.ifnull(status_change_subquery.as_scalar(), self.date_added),
                Recipe.finish_time > nonaborted_recipe_subquery.as_scalar().correlate(None)))\
            .value(func.count(DistroTree.id.distinct()))
        if count >= 2:
            # Broken!
            metrics.increment('counters.suspicious_aborts')
            reason = unicode(_(u'System has a run of aborted recipes '
                    'with reliable distros'))
            log.warn(reason)
            self.mark_broken(reason=reason)

    def reserve_manually(self, service, user=None):
        if user is None:
            user = identity.current.user
        self._check_can_reserve(user)
        if not self.can_reserve_manually(user):
            raise BX(_(u'Cannot manually reserve automated system, '
                    'without borrowing it first. Schedule a job instead'))
        return self._reserve(service, user, u'manual')

    def reserve_for_recipe(self, service, user=None):
        if user is None:
            user = identity.current.user
        self._check_can_reserve(user)
        return self._reserve(service, user, u'recipe')

    def _check_can_reserve(self, user):
        # Throw an exception if the given user can't reserve the system.
        if self.user is not None and self.user == user:
            raise StaleSystemUserException(_(u'User %s has already reserved '
                'system %s') % (user, self))
        if not self.can_reserve(user):
            raise InsufficientSystemPermissions(_(u'User %s cannot '
                'reserve system %s') % (user, self))
        if self.loaned:
            # loans give exclusive rights to reserve
            if user != self.loaned and user != self.owner:
                raise InsufficientSystemPermissions(_(u'User %s cannot reserve '
                        'system %s while it is loaned to user %s')
                        % (user, self, self.loaned))

    def _reserve(self, service, user, reservation_type):
        # Atomic operation to reserve the system
        session.flush()
        if session.connection(System).execute(System.__table__.update(
                and_(System.id == self.id,
                     System.user_id == None)),
                user_id=user.user_id).rowcount != 1:
            raise StaleSystemUserException(_(u'System %r is already '
                'reserved') % self)
        self.user = user # do it here too, so that the ORM is aware
        reservation = Reservation(user=user, type=reservation_type)
        self.reservations.append(reservation)
        self.record_activity(user=user,
                service=service, action=u'Reserved', field=u'User',
                old=u'', new=user.user_name)
        log.debug('Created reservation for system %r with type %r, service %r, user %r',
                self, reservation_type, service, user)
        return reservation

    def unreserve(self, service=None, reservation=None, user=None):
        if user is None:
            user = identity.current.user

        if self.user is None:
            raise BX(_(u'System is not reserved'))
        if not self.can_unreserve(user):
            raise InsufficientSystemPermissions(
                    _(u'User %s cannot unreserve system %s, reserved by %s')
                    % (user, self, self.user))

        # Update reservation atomically first, to avoid races
        session.flush()
        my_reservation_id = reservation.id
        if session.connection(System).execute(Reservation.__table__.update(
                and_(Reservation.id == my_reservation_id,
                     Reservation.finish_time == None)),
                finish_time=datetime.utcnow()).rowcount != 1:
            raise BX(_(u'System does not have an open reservation'))
        session.expire(reservation, ['finish_time'])
        old_user = self.user
        self.user = None
        self.action_release(service=service)
        self.record_activity(user=user,
                service=service, action=u'Returned', field=u'User',
                old=old_user.user_name, new=u'')

    def add_note(self, text, user, service=u'WEBUI'):
        note = Note(user=user, text=text)
        self.notes.append(note)
        self.record_activity(user=user, service=service,
                             action='Added', field='Note',
                             old='', new=text)
        self.date_modified = datetime.utcnow()

    cc = association_proxy('_system_ccs', 'email_address')

    groups = association_proxy('group_assocs', 'group',
            creator=lambda group: SystemGroup(group=group))

@event.listens_for(System.status, 'set', active_history=True, retval=True)
def validate_status(system, child, oldchild, initiator):
    log.debug('%r status changed from %r to %r', system, oldchild, child)
    if child == oldchild:
        return child
    if oldchild in (None, NEVER_SET):
        # First time system.status has been set, there will be no duration 
        # rows yet.
        assert not system.status_durations
        system.status_durations.insert(0, SystemStatusDuration(status=child))
        return child
    # Otherwise, there should be exactly one "open" duration row, 
    # with NULL finish_time.
    open_sd = system.status_durations[0]
    assert open_sd.finish_time is None
    assert open_sd.status == oldchild
    if open_sd in session.new:
        # The current open row is not actually persisted yet. This 
        # happens when system.status is set more than once in 
        # a session. In this case we can just update the same row and 
        # return, no reason to insert another.
        open_sd.status = child
        return child
    # Need to close the open row using a conditional UPDATE to ensure 
    # we don't race with another transaction
    now = datetime.utcnow()
    if session.query(SystemStatusDuration)\
            .filter_by(finish_time=None, id=open_sd.id)\
            .update({'finish_time': now}, synchronize_session=False) \
            != 1:
        raise RuntimeError('System status updated in another transaction')
    # Make the ORM aware of it as well
    open_sd.finish_time = now
    system.status_durations.insert(0, SystemStatusDuration(status=child))
    return child

class SystemCc(DeclarativeMappedObject):

    __tablename__ = 'system_cc'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    system_id = Column(Integer, ForeignKey('system.id', ondelete='CASCADE',
            onupdate='CASCADE'), primary_key=True)
    system = relationship(System, back_populates='_system_ccs')
    email_address = Column(Unicode(255), primary_key=True, index=True)

    def __init__(self, email_address):
        super(SystemCc, self).__init__()
        self.email_address = email_address


class Hypervisor(DeclarativeMappedObject):

    __tablename__ = 'hypervisor'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    hypervisor = Column(Unicode(100), nullable=False)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.hypervisor)

    def __unicode__(self):
        return self.hypervisor

    def __str__(self):
        return unicode(self).encode('utf8')

    def __json__(self):
        return unicode(self)

    @classmethod
    def get_all_types(cls):
        """
        return an array of tuples containing id, hypervisor
        """
        return [(hvisor.id, hvisor.hypervisor) for hvisor in cls.query]

    @classmethod
    def get_all_names(cls):
        return [h.hypervisor for h in cls.query]

    @classmethod
    def by_name(cls, hvisor):
        try:
            return cls.query.filter_by(hypervisor=hvisor).one()
        except NoResultFound:
            raise ValueError('No such hypervisor %r' % hvisor)


class SystemAccessPolicy(DeclarativeMappedObject):

    """
    A list of rules controlling who is allowed to do what to a system.
    """
    __tablename__ = 'system_access_policy'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, nullable=False, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
            name='system_access_policy_system_id_fk'))
    system = relationship(System, back_populates='custom_access_policy')
    rules = relationship('SystemAccessPolicyRule', back_populates='policy',
            cascade='all, delete, delete-orphan')

    def __json__(self):
        return {
            'id': self.id,
            'rules': self.rules,
            'possible_permissions': [
                {'value': unicode(permission),
                 'label': unicode(permission.label)}
                for permission in SystemPermission],
        }

    @classmethod
    def empty_json(cls):
        """
        Returns the JSON representation of a default empty policy, to be used 
        in the cases where a system's policy has not been initialized yet.
        """
        return {
            'id': None,
            'rules': [],
            'possible_permissions': [
                {'value': unicode(permission),
                 'label': unicode(permission.label)}
                for permission in SystemPermission],
        }

    @hybrid_method
    def grants(self, user, permission):
        """
        Does this policy grant the given permission to the given user?
        """
        return any(rule.permission == permission and
                (rule.user == user or rule.group in user.groups or rule.everybody)
                for rule in self.rules)

    @grants.expression
    def grants(cls, user, permission): #pylint: disable=E0213
        # need to avoid passing an empty list to in_
        clauses = [SystemAccessPolicyRule.user == user, SystemAccessPolicyRule.everybody]
        if user.groups:
            clauses.append(SystemAccessPolicyRule.group_id.in_(
                    [g.group_id for g in user.groups]))
        return cls.rules.any(and_(SystemAccessPolicyRule.permission == permission,
                or_(*clauses)))

    @hybrid_method
    def grants_everybody(self, permission):
        """
        Does this policy grant the given permission to all users?
        """
        return any(rule.permission == permission and rule.everybody
                for rule in self.rules)

    @grants_everybody.expression
    def grants_everybody(cls, permission): #pylint: disable=E0213
        return cls.rules.any(and_(SystemAccessPolicyRule.permission == permission,
                SystemAccessPolicyRule.everybody))

    def add_rule(self, permission, user=None, group=None, everybody=False):
        """
        Pass either user, or group, or everybody=True.
        """
        if user is not None and group is not None:
            raise RuntimeError('Rules are for a user or a group, not both')
        if user is None and group is None and not everybody:
            raise RuntimeError('Did you mean to pass everybody=True to add_rule?')
        session.flush() # make sure self is persisted, for lazy_create
        self.rules.append(SystemAccessPolicyRule.lazy_create(policy_id=self.id,
                permission=permission,
                user_id=user.user_id if user else None,
                group_id=group.group_id if group else None))
        return self.rules[-1]

class SystemAccessPolicyRule(DeclarativeMappedObject):

    """
    A single rule in a system access policy. Policies can have one or more of these.

    The existence of a row in this table means that the given permission is 
    granted to the given user or group in this policy.
    """
    __tablename__ = 'system_access_policy_rule'
    __table_args__ = {'mysql_engine': 'InnoDB'}

    # It would be nice to have a constraint like:
    #    UniqueConstraint('policy_id', 'user_id', 'group_id', 'permission')
    # but we can't because user_id and group_id are NULLable and MySQL has
    # non-standard behaviour for that which makes the constraint useless :-(

    id = Column(Integer, nullable=False, primary_key=True)
    policy_id = Column(Integer, ForeignKey('system_access_policy.id',
            name='system_access_policy_rule_policy_id_fk'), nullable=False)
    policy = relationship(SystemAccessPolicy, back_populates='rules')
    # Either user or group is set, to indicate who the rule applies to.
    # If both are NULL, the rule applies to everyone.
    user_id = Column(Integer, ForeignKey('tg_user.user_id',
            name='system_access_policy_rule_user_id_fk'))
    user = relationship(User, back_populates='system_access_policy_rules')
    group_id = Column(Integer, ForeignKey('tg_group.group_id',
            name='system_access_policy_rule_group_id_fk'))
    group = relationship(Group, back_populates='system_access_policy_rules')
    permission = Column(SystemPermission.db_type(), nullable=False)

    def __repr__(self):
        return '<grant %s to %s>' % (self.permission,
                self.group or self.user or 'everybody')

    def __json__(self):
        return {
            'id': self.id,
            'user': self.user.user_name if self.user else None,
            'group': self.group.group_name if self.group else None,
            'everybody': self.everybody,
            'permission': unicode(self.permission),
        }

    @hybrid_property
    def everybody(self):
        return (self.user == None) & (self.group == None)

    def record_deletion(self, service=u'WEBUI'):
        self.policy.system.record_activity(user=identity.current.user, service=service,
                                           action=u'Removed',
                                           field=u'Access Policy Rule', old=repr(self))

class Provision(DeclarativeMappedObject):

    __tablename__ = 'provision'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='provisions')
    ks_meta = Column(String(1024))
    kernel_options = Column(String(1024))
    kernel_options_post = Column(String(1024))
    arch_id = Column(Integer, ForeignKey('arch.id'), nullable=False)
    arch = relationship(Arch)
    provision_families = relationship('ProvisionFamily',
            collection_class=attribute_mapped_collection('osmajor'),
            cascade='all, delete, delete-orphan')


class ProvisionFamily(DeclarativeMappedObject):

    __tablename__ = 'provision_family'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    provision_id = Column(Integer, ForeignKey('provision.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    osmajor_id = Column(Integer, ForeignKey('osmajor.id'), nullable=False)
    osmajor = relationship(OSMajor)
    ks_meta = Column(String(1024))
    kernel_options = Column(String(1024))
    kernel_options_post = Column(String(1024))
    provision_family_updates = relationship('ProvisionFamilyUpdate',
            collection_class=attribute_mapped_collection('osversion'),
            cascade='all, delete, delete-orphan')


class ProvisionFamilyUpdate(DeclarativeMappedObject):

    __tablename__ = 'provision_update_family'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    provision_family_id = Column(Integer, ForeignKey('provision_family.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    osversion_id = Column(Integer, ForeignKey('osversion.id'), nullable=False)
    osversion = relationship(OSVersion)
    ks_meta = Column(String(1024))
    kernel_options = Column(String(1024))
    kernel_options_post = Column(String(1024))


class ExcludeOSMajor(DeclarativeMappedObject):

    __tablename__ = 'exclude_osmajor'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='excluded_osmajor')
    arch_id = Column(Integer, ForeignKey('arch.id'), nullable=False)
    arch = relationship(Arch)
    osmajor_id = Column(Integer, ForeignKey('osmajor.id'), nullable=False)
    osmajor = relationship(OSMajor, back_populates='excluded_osmajors')


class ExcludeOSVersion(DeclarativeMappedObject):

    __tablename__ = 'exclude_osversion'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='excluded_osversion')
    arch_id = Column(Integer, ForeignKey('arch.id'), nullable=False)
    arch = relationship(Arch)
    osversion_id = Column(Integer, ForeignKey('osversion.id'), nullable=False)
    osversion = relationship(OSVersion, back_populates='excluded_osversions')


class LabInfo(DeclarativeMappedObject):

    __tablename__ = 'labinfo'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='labinfo')
    orig_cost = Column(Numeric(precision=16, scale=2, asdecimal=True))
    curr_cost = Column(Numeric(precision=16, scale=2, asdecimal=True))
    dimensions = Column(String(255))
    weight = Column(Numeric(asdecimal=False))
    wattage = Column(Numeric(asdecimal=False))
    cooling = Column(Numeric(asdecimal=False))

    fields = ['orig_cost', 'curr_cost', 'dimensions', 'weight', 'wattage', 'cooling']


class Cpu(DeclarativeMappedObject):

    __tablename__ = 'cpu'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='cpu')
    vendor = Column(String(255))
    model = Column(Integer)
    model_name = Column(String(255))
    family = Column(Integer)
    stepping = Column(Integer)
    speed = Column(Float)
    processors = Column(Integer)
    cores = Column(Integer)
    sockets = Column(Integer)
    hyper = Column(Boolean)
    flags = relationship('CpuFlag', cascade='all, delete, delete-orphan')

    def __init__(self, vendor=None, model=None, model_name=None, family=None, stepping=None,speed=None,processors=None,cores=None,sockets=None,flags=None):
        super(Cpu, self).__init__()
        self.vendor = vendor
        self.model = model
        self.model_name = model_name
        self.family = family
        self.stepping = stepping
        self.speed = speed
        self.processors = processors
        self.cores = cores
        self.sockets = sockets
        if self.processors > self.cores:
            self.hyper = True
        else:
            self.hyper = False
        self.updateFlags(flags)

    def updateFlags(self,flags):
        if flags != None:
            for cpuflag in flags:
                new_flag = CpuFlag(flag=cpuflag)
                self.flags.append(new_flag)

class CpuFlag(DeclarativeMappedObject):

    __tablename__ = 'cpu_flag'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    cpu_id = Column(Integer, ForeignKey('cpu.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    flag = Column(String(255))

    def __init__(self, flag=None):
        super(CpuFlag, self).__init__()
        self.flag = flag

    def __repr__(self):
        return self.flag

    def by_flag(cls, flag):
        return cls.query.filter_by(flag=flag)

    by_flag = classmethod(by_flag)


class Numa(DeclarativeMappedObject):

    __tablename__ = 'numa'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='numa')
    nodes = Column(Integer)

    def __init__(self, nodes=None):
        super(Numa, self).__init__()
        self.nodes = nodes

    def __repr__(self):
        return str(self.nodes)


class DeviceClass(DeclarativeMappedObject):

    __tablename__ = 'device_class'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    device_class = Column(VARCHAR(24), nullable=False, unique=True)
    description = Column(TEXT)

    @classmethod
    def lazy_create(cls, device_class=None, **kwargs):
        """
        Like the normal lazy_create, but with special handling for
        device_class None -> "NONE".
        """
        if not device_class:
            device_class = 'NONE'
        return super(DeviceClass, cls).lazy_create(
                device_class=device_class, **kwargs)

    def __init__(self, device_class=None, description=None):
        super(DeviceClass, self).__init__()
        if not device_class:
            device_class = "NONE"
        self.device_class = device_class
        self.description = description

    def __repr__(self):
        return self.device_class


class Device(DeclarativeMappedObject):

    __tablename__ = 'device'
    __table_args__ = (
        UniqueConstraint('vendor_id', 'device_id', 'subsys_device_id',
               'subsys_vendor_id', 'bus', 'driver', 'description',
               'device_class_id', name='device_uix_1'),
        {'mysql_engine': 'InnoDB'}
    )
    id = Column(Integer, autoincrement=True, primary_key=True)
    vendor_id = Column(String(4))
    device_id = Column(String(4))
    subsys_device_id = Column(String(4))
    subsys_vendor_id = Column(String(4))
    bus = Column(String(255))
    driver = Column(String(255), index=True)
    description = Column(String(255))
    device_class_id = Column(Integer, ForeignKey('device_class.id'), nullable=False)
    device_class = relationship(DeviceClass)
    date_added = Column(DateTime, default=datetime.utcnow, nullable=False)
    systems = relationship(System, secondary=system_device_map, back_populates='devices')

Index('ix_device_pciid', Device.vendor_id, Device.device_id)

class Disk(DeclarativeMappedObject):

    __tablename__ = 'disk'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id'), nullable=False)
    system = relationship(System, back_populates='disks')
    model = Column(String(255))
    # sizes are in bytes
    size = Column(BigInteger)
    sector_size = Column(Integer)
    phys_sector_size = Column(Integer)

    def __init__(self, size=None, sector_size=None, phys_sector_size=None, model=None):
        super(Disk, self).__init__()
        self.size = int(size)
        self.sector_size = int(sector_size)
        self.phys_sector_size = int(phys_sector_size)
        self.model = model

class PowerType(DeclarativeMappedObject):

    __tablename__ = 'power_type'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    name = Column(String(255), nullable=False)

    def __init__(self, name=None):
        super(PowerType, self).__init__()
        self.name = name

    @classmethod
    def get_all(cls):
        """
        Apc, wti, etc..
        """
        all_types = cls.query
        return [(0, "None")] + [(type.id, type.name) for type in all_types]

    @classmethod
    def by_name(cls, name):
        return cls.query.filter_by(name=name).one()

    @classmethod
    def by_id(cls, id):
        return cls.query.filter_by(id=id).one()

    @classmethod
    def list_by_name(cls,name,find_anywhere=False):
        if find_anywhere:
            q = cls.query.filter(PowerType.name.like('%%%s%%' % name))
        else:
            q = cls.query.filter(PowerType.name.like('%s%%' % name))
        return q

class Power(DeclarativeMappedObject):

    __tablename__ = 'power'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    # 5(seconds) was the default sleep time for commands in beaker-provision
    default_quiescent_period = 5
    id = Column(Integer, autoincrement=True, primary_key=True)
    power_type_id = Column(Integer, ForeignKey('power_type.id'), nullable=False)
    power_type = relationship(PowerType)
    system_id = Column(Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False)
    system = relationship(System, back_populates='power')
    power_address = Column(String(255), nullable=False)
    power_user = Column(String(255))
    power_passwd = Column(String(255))
    power_id = Column(String(255))
    power_quiescent_period = Column(Integer, default=default_quiescent_period,
        nullable=False)

    def __json__(self):
        return {
            'id': self.id,
            'power_type': self.power_type.name,
            'power_address': self.power_address,
            'power_user': self.power_user,
            'power_password': self.power_passwd,
            'power_id': self.power_id,
            'power_quiescent_period': self.power_quiescent_period,
            'possible_power_types': [t.name for t in PowerType.query.order_by(PowerType.name)],
        }

    @classmethod
    def empty_json(cls):
        """
        Returns the JSON representation of a default empty power config, to be 
        used in the cases where a system's power config has not been 
        initialized yet.
        """
        return {
            'power_type': None,
            'power_address': None,
            'power_user': None,
            'power_password': None,
            'power_id': None,
            'power_quiescent_period': cls.default_quiescent_period,
            'possible_power_types': [t.name for t in PowerType.query.order_by(PowerType.name)],
        }

# note model
class Note(DeclarativeMappedObject):

    __tablename__ = 'note'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id', onupdate='CASCADE',
            ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('tg_user.user_id'), index=True)
    user = relationship(User, back_populates='notes')
    created = Column(DateTime, nullable=False, default=datetime.utcnow)
    text = Column(TEXT, nullable=False)
    deleted = Column(DateTime, nullable=True, default=None)
    system = relationship(System, back_populates='notes')

    def __init__(self, user=None, text=None):
        super(Note, self).__init__()
        self.user = user
        self.text = text

    @classmethod
    def all(cls):
        return cls.query

    @property
    def html(self):
        """
        The note's text rendered to HTML using Markdown.
        """
        # Try rendering as markdown, if that fails for any reason, just
        # return the raw text string. The template will take care of the
        # difference (this really doesn't belong in the model, though...)
        try:
            rendered = markdown(self.text, safe_mode='escape')
        except Exception:
            return self.text
        return XML(rendered)


class Key(DeclarativeMappedObject):

    __tablename__ = 'key_'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    key_name = Column(String(50), nullable=False, unique=True)
    numeric = Column(Boolean, default=False)
    key_value_string = relationship('Key_Value_String',
            back_populates='key', cascade='all, delete-orphan')
    key_value_int = relationship('Key_Value_Int',
            back_populates='key', cascade='all, delete-orphan')

    # Obsoleted keys are ones which have been replaced by real, structured 
    # columns on the system table (and its related tables). We disallow users 
    # from searching on these keys in the web UI, to encourage them to migrate 
    # to the structured columns instead (and to avoid the costly queries that 
    # sometimes result).
    obsoleted_keys = [u'MODULE', u'PCIID']

    @classmethod
    def get_all_keys(cls):
        """
        This method's name is deceptive, it actually excludes "obsoleted" keys.
        """
        all_keys = cls.query.order_by(Key.key_name)
        return [key.key_name for key in all_keys
                if key.key_name not in cls.obsoleted_keys]

    @classmethod
    def by_name(cls, key_name):
        return cls.query.filter_by(key_name=key_name).one()


    @classmethod
    def list_by_name(cls, name, find_anywhere=False):
        """
        A class method that can be used to search keys
        based on the key_name
        """
        if find_anywhere:
            q = cls.query.filter(Key.key_name.like('%%%s%%' % name))
        else:
            q = cls.query.filter(Key.key_name.like('%s%%' % name))
        return q

    @classmethod
    def by_id(cls, id):
        return cls.query.filter_by(id=id).one()

    def __init__(self, key_name=None, numeric=False):
        super(Key, self).__init__()
        self.key_name = key_name
        self.numeric = numeric

    def __repr__(self):
        return "%s" % self.key_name


# key_value model
class Key_Value_String(DeclarativeMappedObject):

    __tablename__ = 'key_value_string'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id', onupdate='CASCADE',
            ondelete='CASCADE'), nullable=False, index=True)
    system = relationship(System, back_populates='key_values_string')
    key_id = Column(Integer, ForeignKey('key_.id', onupdate='CASCADE',
            ondelete='CASCADE'), nullable=False, index=True)
    key = relationship(Key, back_populates='key_value_string')
    key_value = Column(TEXT, nullable=False)

    key_type = 'string'

    def __init__(self, key, key_value, system=None):
        super(Key_Value_String, self).__init__()
        self.system = system
        self.key = key
        self.key_value = key_value

    def __repr__(self):
        return "%s %s" % (self.key, self.key_value)

    @classmethod
    def by_key_value(cls, system, key, value):
        return cls.query.filter(and_(Key_Value_String.key==key,
                                  Key_Value_String.key_value==value,
                                  Key_Value_String.system==system)).one()


class Key_Value_Int(DeclarativeMappedObject):

    __tablename__ = 'key_value_int'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, autoincrement=True, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id', onupdate='CASCADE',
            ondelete='CASCADE'), nullable=False, index=True)
    system = relationship(System, back_populates='key_values_int')
    key_id = Column(Integer, ForeignKey('key_.id', onupdate='CASCADE',
            ondelete='CASCADE'), nullable=False, index=True)
    key = relationship(Key, back_populates='key_value_int')
    key_value = Column(Integer, nullable=False)

    key_type = 'int'

    def __init__(self, key, key_value, system=None):
        super(Key_Value_Int, self).__init__()
        self.system = system
        self.key = key
        self.key_value = key_value

    def __repr__(self):
        return "%s %s" % (self.key, self.key_value)

    @classmethod
    def by_key_value(cls, system, key, value):
        return cls.query.filter(and_(Key_Value_Int.key==key,
                                  Key_Value_Int.key_value==value,
                                  Key_Value_Int.system==system)).one()

# available in python 2.7+ importlib
def import_module(modname):
    __import__(modname)
    return sys.modules[modname]

