#
# Copyright (c) 2017 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import collections
import copy
import six
import uuid

from netaddr import IPAddress
from netaddr import IPNetwork

from sysinv.common import constants
from sysinv.common import exception
from sysinv.common import utils
from sysinv.conductor import openstack
from sysinv.openstack.common import log

from . import base


LOG = log.getLogger(__name__)

PLATFORM_NETWORK_TYPES = [constants.NETWORK_TYPE_PXEBOOT,
                          constants.NETWORK_TYPE_MGMT,
                          constants.NETWORK_TYPE_INFRA,
                          constants.NETWORK_TYPE_OAM,
                          constants.NETWORK_TYPE_DATA_VRS,  # For HP/Nuage
                          constants.NETWORK_TYPE_BM,  # For internal use only
                          constants.NETWORK_TYPE_CONTROL]

DATA_NETWORK_TYPES = [constants.NETWORK_TYPE_DATA]

PCI_NETWORK_TYPES = [constants.NETWORK_TYPE_PCI_SRIOV,
                     constants.NETWORK_TYPE_PCI_PASSTHROUGH]

ACTIVE_STANDBY_AE_MODES = ['active_backup', 'active-backup', 'active_standby']
BALANCED_AE_MODES = ['balanced', 'balanced-xor']
LACP_AE_MODES = ['802.3ad']

DRIVER_MLX_CX3 = 'mlx4_core'
DRIVER_MLX_CX4 = 'mlx5_core'

MELLANOX_DRIVERS = [DRIVER_MLX_CX3,
                    DRIVER_MLX_CX4]

LOOPBACK_IFNAME = 'lo'
LOOPBACK_METHOD = 'loopback'

NETWORK_CONFIG_RESOURCE = 'platform::interfaces::network_config'
ROUTE_CONFIG_RESOURCE = 'platform::interfaces::route_config'
ADDRESS_CONFIG_RESOURCE = 'platform::addresses::address_config'


class InterfacePuppet(base.BasePuppet):
    """Class to encapsulate puppet operations for interface configuration"""

    def __init__(self, *args, **kwargs):
        super(InterfacePuppet, self).__init__(*args, **kwargs)
        self._openstack = None

    @property
    def openstack(self):
        if not self._openstack:
            self._openstack = openstack.OpenStackOperator(self.dbapi)
        return self._openstack

    def get_host_config(self, host):
        """
        Generate the hiera data for the puppet network config and route config
        resources for the host.
        """

        # Normalize some of the host info into formats that are easier to
        # use when parsing the interface list.
        context = self._create_interface_context(host)

        if host.personality == constants.CONTROLLER:
            # Insert a fake BMC interface because BMC information is only
            # stored on the host and in the global config.  This makes it
            # easier to setup the BMC interface from the interface handling
            # code.  Hopefully we can add real interfaces in the DB some day
            # and remove this code.
            self._create_bmc_interface(host, context)

        # interface configuration is organized into sets of network_config,
        # route_config and address_config resource hashes (dict)
        config = {
            NETWORK_CONFIG_RESOURCE: {},
            ROUTE_CONFIG_RESOURCE: {},
            ADDRESS_CONFIG_RESOURCE: {},
        }

        system = self._get_system()
        # For AIO-SX subcloud, mgmt n/w will be on a separate
        # physical interface instead of the loopback interface.
        if system.system_mode != constants.SYSTEM_MODE_SIMPLEX or \
                self._distributed_cloud_role() == \
                constants.DISTRIBUTED_CLOUD_ROLE_SUBCLOUD:
            # Setup the loopback interface first
            generate_loopback_config(config)

        # Generate the actual interface config resources
        generate_interface_configs(context, config)

        # Generate the actual interface config resources
        generate_address_configs(context, config)

        # Generate driver specific configuration
        generate_driver_config(context, config)

        # Generate the dhcp client configuration
        generate_dhcp_config(context, config)

        # Update the global context with generated interface context
        self.context.update(context)

        return config

    def _create_interface_context(self, host):
        context = {
            'hostname': host.hostname,
            'personality': host.personality,
            'subfunctions': host.subfunctions,
            'system_uuid': host.isystem_uuid,
            'ports': self._get_port_interface_id_index(host),
            'interfaces': self._get_interface_name_index(host),
            'devices': self._get_port_pciaddr_index(host),
            'addresses': self._get_address_interface_name_index(host),
            'routes': self._get_routes_interface_name_index(host),
            'networks': self._get_network_type_index(),
            'gateways': self._get_gateway_index(),
            'floatingips': self._get_floating_ip_index(),
            'providernets': self._get_provider_networks(host),
        }
        return context

    def _create_bmc_interface(self, host, context):
        """
        Creates a fake BMC interface and inserts it into the context interface
        list.  It also creates a fake BMC address and inserts it into the
        context address list.  This is required because these two entities
        exist only as attributes on the host and in local context variables.
        Rather than have different code to generate manifest entries based on
        these other data structures it is easier to create fake context entries
        and re-use the existing code base.
        """
        try:
            network = self.dbapi.network_get_by_type(
                constants.NETWORK_TYPE_BM)
        except exception.NetworkTypeNotFound:
            # No BMC network configured
            return

        lower_iface = _find_bmc_lower_interface(context)
        if not lower_iface:
            # No mgmt or pxeboot?
            return

        addr = self._get_address_by_name(host.hostname,
                                         constants.NETWORK_TYPE_BM)

        iface = {
            'uuid': str(uuid.uuid4()),
            'ifname': 'bmc0',
            'iftype': constants.INTERFACE_TYPE_VLAN,
            'networktype': constants.NETWORK_TYPE_BM,
            'imtu': network.mtu,
            'vlan_id': network.vlan_id,
            'uses': [lower_iface['ifname']],
            'used_by': []
        }

        lower_iface['used_by'] = ['bmc0']
        address = {
            'ifname': iface['ifname'],
            'family': addr.family,
            'prefix': addr.prefix,
            'address': addr.address,
            'networktype': iface['networktype']
        }

        context['interfaces'].update({iface['ifname']: iface})
        context['addresses'].update({iface['ifname']: [address]})

    def _find_host_interface(self, host, networktype):
        """
        Search the host interface list looking for an interface with a given
        primary network type.
        """
        for iface in self.dbapi.iinterface_get_by_ihost(host.id):
            if networktype == utils.get_primary_network_type(iface):
                return iface

    def _get_port_interface_id_index(self, host):
        """
        Builds a dictionary of ports indexed by interface id.
        """
        ports = {}
        for port in self.dbapi.ethernet_port_get_by_host(host.id):
            ports[port.interface_id] = port
        return ports

    def _get_interface_name_index(self, host):
        """
        Builds a dictionary of interfaces indexed by interface name.
        """
        interfaces = {}
        for iface in self.dbapi.iinterface_get_by_ihost(host.id):
            interfaces[iface.ifname] = iface
        return interfaces

    def _get_port_pciaddr_index(self, host):
        """
        Builds a dictionary of port lists indexed by PCI address.
        """
        devices = collections.defaultdict(list)
        for port in self.dbapi.ethernet_port_get_by_host(host.id):
            devices[port.pciaddr].append(port)
        return devices

    def _get_address_interface_name_index(self, host):
        """
        Builds a dictionary of address lists indexed by interface name.
        """
        addresses = collections.defaultdict(list)
        for address in self.dbapi.addresses_get_by_host(host.id):
            addresses[address.ifname].append(address)
        return addresses

    def _get_routes_interface_name_index(self, host):
        """
        Builds a dictionary of route lists indexed by interface name.
        """
        routes = collections.defaultdict(list)
        for route in self.dbapi.routes_get_by_host(host.id):
            routes[route.ifname].append(route)

        results = collections.defaultdict(list)
        for ifname, entries in six.iteritems(routes):
            entries = sorted(entries, key=lambda r: r['prefix'], reverse=True)
            results[ifname] = entries
        return results

    def _get_network_type_index(self):
        networks = {}
        for network in self.dbapi.networks_get_all():
            networks[network['type']] = network
        return networks

    def _get_gateway_index(self):
        """
        Builds a dictionary of gateway IP addresses indexed by network type.
        """
        gateways = {}
        try:
            mgmt_address = self._get_address_by_name(
                constants.CONTROLLER_GATEWAY, constants.NETWORK_TYPE_MGMT)
            gateways.update({
                constants.NETWORK_TYPE_MGMT: mgmt_address.address})
        except exception.AddressNotFoundByName:
            pass

        try:
            oam_address = self._get_address_by_name(
                constants.CONTROLLER_GATEWAY, constants.NETWORK_TYPE_OAM)
            gateways.update({
                constants.NETWORK_TYPE_OAM: oam_address.address})
        except exception.AddressNotFoundByName:
            pass

        return gateways

    def _get_floating_ip_index(self):
        """
        Builds a dictionary of floating ip addresses indexed by network type.
        """
        mgmt_address = self._get_address_by_name(
            constants.CONTROLLER_HOSTNAME, constants.NETWORK_TYPE_MGMT)

        mgmt_floating_ip = (str(mgmt_address.address) + '/' +
                            str(mgmt_address.prefix))

        floating_ips = {
            constants.NETWORK_TYPE_MGMT: mgmt_floating_ip
        }

        try:
            pxeboot_address = self._get_address_by_name(
                constants.CONTROLLER_HOSTNAME, constants.NETWORK_TYPE_PXEBOOT)

            pxeboot_floating_ip = (str(pxeboot_address.address) + '/' +
                                   str(pxeboot_address.prefix))

            floating_ips.update({
                constants.NETWORK_TYPE_PXEBOOT: pxeboot_floating_ip,
            })
        except exception.AddressNotFoundByName:
            pass

        system = self._get_system()
        if system.system_mode != constants.SYSTEM_MODE_SIMPLEX:
            oam_address = self._get_address_by_name(
                constants.CONTROLLER_HOSTNAME, constants.NETWORK_TYPE_OAM)

            oam_floating_ip = (str(oam_address.address) + '/' +
                               str(oam_address.prefix))

            floating_ips.update({
                constants.NETWORK_TYPE_OAM: oam_floating_ip
            })

        return floating_ips

    def _get_provider_networks(self, host):
        # TODO(alegacy): this will not work as intended for upgrades of AIO-SX
        # and -DX.  The call to get_providernetworksdict will return an empty
        # dictionary because the neutron endpoint is not available yet.  Since
        # we do not currently support SDN/OVS over upgrades we will need to
        # deal with this in a later commit.
        pnets = {}
        if (self.openstack and
                constants.COMPUTE in utils.get_personalities(host)):
            pnets = self.openstack.get_providernetworksdict(quiet=True)
        return pnets


def is_platform_network_type(iface):
    networktype = utils.get_primary_network_type(iface)
    return bool(networktype in PLATFORM_NETWORK_TYPES)


def is_data_network_type(iface):
    networktypelist = utils.get_network_type_list(iface)
    return bool(any(n in DATA_NETWORK_TYPES for n in networktypelist))


def _find_bmc_lower_interface(context):
    """
    Search the profile interface list looking for either a pxeboot or mgmt
    interface that can be used to attach a BMC VLAN interface.  If a pxeboot
    interface exists then it is preferred since we do not want to create a VLAN
    over another VLAN interface.
    """
    selected_iface = None
    for ifname, iface in six.iteritems(context['interfaces']):
        networktype = utils.get_primary_network_type(iface)
        if networktype == constants.NETWORK_TYPE_PXEBOOT:
            return iface
        elif networktype == constants.NETWORK_TYPE_MGMT:
            selected_iface = iface
    return selected_iface


def is_controller(context):
    """
    Determine we are creating a manifest for a controller node; regardless of
    whether it has a compute subfunction or not.
    """
    return bool(context['personality'] == constants.CONTROLLER)


def is_compute_subfunction(context):
    """
    Determine if we are creating a manifest for a compute node or a compute
    subfunction.
    """
    if context['personality'] == constants.COMPUTE:
        return True
    if constants.COMPUTE in context['subfunctions']:
        return True
    return False


def is_pci_interface(iface):
    """
    Determine if the interface is one of the PCI device types.
    """
    networktype = utils.get_primary_network_type(iface)
    return bool(networktype in PCI_NETWORK_TYPES)


def is_platform_interface(context, iface):
    """
    Determine whether the interface needs to be configured in the linux kernel
    as opposed to interfaces that exist purely in the vswitch.  This includes
    interfaces that are themselves platform interfaces or interfaces that have
    platform interfaces above them.  Both of these groups of interfaces require
    a linux interface that will be used for platform purposes (i.e., pxeboot,
    mgmt, infra, oam).
    """
    if '_kernel' in iface:  # check cached result
        return iface['_kernel']
    else:
        kernel = False
        if is_platform_network_type(iface):
            kernel = True
        else:
            upper_ifnames = iface['used_by'] or []
            for upper_ifname in upper_ifnames:
                upper_iface = context['interfaces'][upper_ifname]
                if is_platform_interface(context, upper_iface):
                    kernel = True
                    break
    iface['_kernel'] = kernel  # cache the result
    return iface['_kernel']


def is_data_interface(context, iface):
    """
    Determine whether the interface needs to be configured in the vswitch.
    This includes interfaces that are themselves data interfaces or interfaces
    that have data interfaces above them.  Both of these groups of interfaces
    require vswitch configuration data.
    """
    if '_data' in iface:  # check cached result
        return iface['_data']
    else:
        data = False
        if is_data_network_type(iface):
            data = True
        else:
            upper_ifnames = iface['used_by'] or []
            for upper_ifname in upper_ifnames:
                upper_iface = context['interfaces'][upper_ifname]
                if is_data_interface(context, upper_iface):
                    data = True
                    break
    iface['_data'] = data  # cache the result
    return iface['_data']


def is_dpdk_compatible(context, iface):
    """
    Determine whether an interface can be supported in vswitch as a native DPDK
    interface.  Since whether an interface is supported or not by the DPDK
    means whether the DPDK has a hardware device driver for the underlying
    physical device this also implies that all non-hardware related interfaces
    are automatically supported in the DPDK.  For this reason we report True
    for VLAN and AE interfaces but check the DPDK support status for any
    ethernet interfaces.
    """
    if '_dpdksupport' in iface:  # check the cached result
        return iface['_dpdksupport']
    elif iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET:
        port = get_interface_port(context, iface)
        dpdksupport = port.get('dpdksupport', False)
    else:
        dpdksupport = True
    iface['_dpdksupport'] = dpdksupport  # cache the result
    return iface['_dpdksupport']


def is_a_mellanox_device(context, iface):
    """
    Determine if the underlying device is a Mellanox device.
    """
    if iface['iftype'] != constants.INTERFACE_TYPE_ETHERNET:
        # We only care about configuring specific settings for related ethernet
        # devices.
        return False
    port = get_interface_port(context, iface)
    if port['driver'] in MELLANOX_DRIVERS:
        return True
    return False


def is_a_mellanox_cx3_device(context, iface):
    """
    Determine if the underlying device is a Mellanox CX3 device.
    """
    if iface['iftype'] != constants.INTERFACE_TYPE_ETHERNET:
        # We only care about configuring specific settings for related ethernet
        # devices.
        return False
    port = get_interface_port(context, iface)
    if port['driver'] == DRIVER_MLX_CX3:
        return True
    return False


def get_master_interface(context, iface):
    """
    Get the interface name of the given interface's master (if any).  The
    master interface is the AE interface for any Ethernet interfaces.
    """
    if '_master' not in iface:  # check the cached result
        master = None
        if iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET:
            upper_ifnames = iface['used_by'] or []
            for upper_ifname in upper_ifnames:
                upper_iface = context['interfaces'][upper_ifname]
                if upper_iface['iftype'] == constants.INTERFACE_TYPE_AE:
                    master = upper_iface['ifname']
                    break
        iface['_master'] = master  # cache the result
    return iface['_master']


def is_slave_interface(context, iface):
    """
    Determine if this interface is a slave interface.  A slave interface is an
    interface that is part of an AE interface.
    """
    if '_slave' not in iface:  # check the cached result
        master = get_master_interface(context, iface)
        iface['_slave'] = bool(master)  # cache the result
    return iface['_slave']


def get_interface_mtu(context, iface):
    """
    Determine the MTU value to use for a given interface.  We trust that sysinv
    has selected the correct value.
    """
    return iface['imtu']


def get_interface_providernets(iface):
    """
    Return the provider networks of the supplied interface as a list.
    """
    providernetworks = iface['providernetworks']
    if not providernetworks:
        return []
    return [x.strip() for x in providernetworks.split(',')]


def get_interface_port(context, iface):
    """
    Determine the port of the underlying device.
    """
    assert iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET
    return context['ports'][iface['id']]


def get_interface_port_name(context, iface):
    """
    Determine the port name of the underlying device.
    """
    assert iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET
    port = get_interface_port(context, iface)
    if port:
        return port['name']


def get_lower_interface(context, iface):
    """
    Return the interface object that is used to implement a VLAN interface.
    """
    assert iface['iftype'] == constants.INTERFACE_TYPE_VLAN
    lower_ifname = iface['uses'][0]
    return context['interfaces'][lower_ifname]


def get_lower_interface_os_ifname(context, iface):
    """
    Return the kernel interface name of the lower interface used to implement a
    VLAN interface.
    """
    lower_iface = get_lower_interface(context, iface)
    return get_interface_os_ifname(context, lower_iface)


def get_interface_os_ifname(context, iface):
    """
    Determine the interface name used in the linux kernel for the given
    interface.  Ethernet interfaces uses the original linux device name while
    AE devices can use the user-defined named.  VLAN interface must derive
    their names based on their lower interface name.
    """
    if '_os_ifname' in iface:  # check cached result
        return iface['_os_ifname']
    else:
        os_ifname = iface['ifname']
        if iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET:
            os_ifname = get_interface_port_name(context, iface)
        elif iface['iftype'] == constants.INTERFACE_TYPE_VLAN:
            lower_os_ifname = get_lower_interface_os_ifname(context, iface)
            os_ifname = lower_os_ifname + "." + str(iface['vlan_id'])
        elif iface['iftype'] == constants.INTERFACE_TYPE_AE:
            os_ifname = iface['ifname']
        iface['_os_ifname'] = os_ifname  # cache the result
        return iface['_os_ifname']


def get_interface_routes(context, iface):
    """
    Determine the list of routes that are applicable to a given interface (if
    any).
    """
    return context['routes'][iface['ifname']]


def get_network_speed(context, networktype):
    if 'networks' in context:
        network = context['networks'].get(networktype, None)
        if network:
            return network['link_capacity']
    return 0


def _set_address_netmask(address):
    """
    The netmask is not supplied by sysinv but is required by the puppet
    resource class.
    """
    network = IPNetwork(address['address'] + '/' + str(address['prefix']))
    if network.version == 6:
        address['netmask'] = str(network.prefixlen)
    else:
        address['netmask'] = str(network.netmask)
    return address


def get_interface_primary_address(context, iface):
    """
    Determine the primary IP address on an interface (if any).  If multiple
    addresses exist then the first address is returned.
    """
    addresses = context['addresses'].get(iface['ifname'], [])
    if len(addresses) > 0:
        return _set_address_netmask(addresses[0])


def get_interface_address_family(context, iface):
    """
    Determine the IP family/version of the interface primary address.  If there
    is no address then the IPv4 family identifier is returned so that an
    appropriate default is always present in interface configurations.
    """
    address = get_interface_primary_address(context, iface)
    if not address:
        return 'inet'  # default to ipv4
    elif IPAddress(address['address']).version == 4:
        return 'inet'
    else:
        return 'inet6'


def get_interface_gateway_address(context, iface):
    """
    Determine if the interface has a default gateway.
    """
    networktype = utils.get_primary_network_type(iface)
    return context['gateways'].get(networktype, None)


def get_interface_address_method(context, iface):
    """
    Determine what type of interface to configure for each network type.
    """
    networktype = utils.get_primary_network_type(iface)
    if not networktype:
        # Interfaces that are configured purely as a dependency from other
        # interfaces (i.e., vlan lower interface, bridge member, bond slave)
        # should be left as manual config
        return 'manual'
    elif networktype in DATA_NETWORK_TYPES:
        # All data interfaces configured in the kernel because they are not
        # natively supported in vswitch or need to be shared with the kernel
        # because of a platform VLAN should be left as manual config
        return 'manual'
    elif networktype == constants.NETWORK_TYPE_CONTROL:
        return 'static'
    elif networktype == constants.NETWORK_TYPE_DATA_VRS:
        # All HP/Nuage interfaces have their own IP address defined statically
        return 'static'
    elif networktype == constants.NETWORK_TYPE_BM:
        return 'static'
    elif networktype in PCI_NETWORK_TYPES:
        return 'manual'
    else:
        if is_controller(context):
            # All other interface types that exist on a controller are setup
            # statically since the controller themselves run the DHCP server.
            return 'static'
        elif networktype == constants.NETWORK_TYPE_PXEBOOT:
            # All pxeboot interfaces that exist on non-controller nodes are set
            # to manual as they are not needed/used once the install is done.
            # They exist only in support of the vlan mgmt interface above it.
            return 'manual'
        else:
            # All other types get their addresses from the controller
            return 'dhcp'


def get_interface_traffic_classifier(context, iface):
    """
    Get the interface traffic classifier command line (if any)
    """
    networktype = utils.get_primary_network_type(iface)
    if networktype in [constants.NETWORK_TYPE_MGMT,
                       constants.NETWORK_TYPE_INFRA]:
        networkspeed = get_network_speed(context, networktype)
        return '/usr/local/bin/cgcs_tc_setup.sh %s %s %s > /dev/null' % (
            get_interface_os_ifname(context, iface),
            networktype,
            networkspeed)
    return None


def get_bridge_interface_name(context, iface):
    """
    If the given interface is a bridge member then retrieve the bridge
    interface name otherwise return None.
    """
    if '_bridge' in iface:  # check the cached result
        return iface['_bridge']
    else:
        bridge = None
        if (iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET and
                is_data_interface(context, iface) and
                not is_dpdk_compatible(context, iface)):
            bridge = 'br-' + get_interface_os_ifname(context, iface)
        iface['_bridge'] = bridge  # cache the result
        return iface['_bridge']


def is_bridged_interface(context, iface):
    """
    Determine if this interface is a member of a bridge.  A interface is a
    member of a bridge if the interface is a data interface that is not
    accelerated (i.e., a slow data interface).
    """
    if '_bridged' in iface:  # check the cached result
        return iface['_bridged']
    else:
        bridge = get_bridge_interface_name(context, iface)
        iface['_bridged'] = bool(bridge)  # cache the result
        return iface['_bridged']


def needs_interface_config(context, iface):
    """
    Determine whether an interface needs to be configured in the linux kernel.
    This is true if the interface is a platform interface, is required by a
    platform interface (i.e., an AE member, a VLAN lower interface), or is an
    unaccelerated data interface.
    """
    if is_platform_interface(context, iface):
        return True
    elif not is_compute_subfunction(context):
        return False
    elif is_data_interface(context, iface):
        if not is_dpdk_compatible(context, iface):
            # vswitch interfaces for devices that are not natively supported by
            # the DPDK are created as regular Linux devices and then bridged in
            # to vswitch in order for it to be able to use it indirectly.
            return True
        if is_a_mellanox_device(context, iface):
            # Check for Mellanox data interfaces. We must set the MTU sizes of
            # Mellanox data interfaces in case it is not the default.  Normally
            # data interfaces are owned by DPDK, they are not managed through
            # Linux but in the Mellanox case, the interfaces are still visible
            # in Linux so in case one needs to set jumbo frames, it has to be
            # set in Linux as well. We only do this for combined nodes or
            # non-controller nodes.
            return True
    elif is_pci_interface(iface):
        return True
    return False


def get_basic_network_config(ifname, ensure='present',
                             method='manual', onboot='true',
                             hotplug='false', family='inet',
                             mtu=None):
    """
    Builds a basic network config dictionary with all of the fields required to
    format a basic network_config puppet resource.
    """
    config = {'ifname': ifname,
              'ensure': ensure,
              'family': family,
              'method': method,
              'hotplug': hotplug,
              'onboot': onboot,
              'options': {}}
    if mtu:
        config['mtu'] = str(mtu)
    return config


def get_bridge_network_config(context, iface):
    """
    Builds a network config dictionary for bridge interface resource.
    """
    os_ifname = get_interface_os_ifname(context, iface)
    os_ifname = 'br-' + os_ifname
    method = get_interface_address_method(context, iface)
    family = get_interface_address_family(context, iface)
    config = get_basic_network_config(
        os_ifname, method=method, family=family)
    config['options']['TYPE'] = 'Bridge'
    return config


def get_vlan_network_config(context, iface, config):
    """
    Augments a basic config dictionary with the attributes specific to a VLAN
    interface.
    """
    options = {'VLAN': 'yes',
               'pre_up': '/sbin/modprobe -q 8021q'}
    config['options'].update(options)
    return config


def get_bond_interface_options(iface):
    """
    Get the interface config attribute for bonding options
    """
    ae_mode = iface['aemode']
    tx_hash_policy = iface['txhashpolicy']
    options = None
    if ae_mode in ACTIVE_STANDBY_AE_MODES:
        options = 'mode=active-backup miimon=100'
    else:
        options = 'xmit_hash_policy=%s miimon=100' % tx_hash_policy
        if ae_mode in BALANCED_AE_MODES:
            options = 'mode=balance-xor ' + options
        elif ae_mode in LACP_AE_MODES:
            options = 'mode=802.3ad lacp_rate=fast ' + options
    return options


def get_bond_network_config(context, iface, config):
    """
    Augments a basic config dictionary with the attributes specific to a bond
    interface.
    """
    options = {'MACADDR': iface['imac'].rstrip()}
    bonding_options = get_bond_interface_options(iface)
    if bonding_options:
        options['BONDING_OPTS'] = bonding_options
        options['up'] = 'sleep 10'
    config['options'].update(options)
    return config


def get_ethernet_network_config(context, iface, config):
    """
    Augments a basic config dictionary with the attributes specific to an
    ethernet interface.
    """
    networktype = utils.get_primary_network_type(iface)
    options = {}
    # Increased to accommodate devices that require more time to
    # complete link auto-negotiation
    options['LINKDELAY'] = '20'
    if is_bridged_interface(context, iface):
        options['BRIDGE'] = get_bridge_interface_name(context, iface)
    elif is_slave_interface(context, iface):
        options['SLAVE'] = 'yes'
        options['MASTER'] = get_master_interface(context, iface)
        options['PROMISC'] = 'yes'
    elif networktype == constants.NETWORK_TYPE_PCI_SRIOV:
        if not is_a_mellanox_cx3_device(context, iface):
            # CX3 device can only use kernel module options to enable vfs
            # others share the same pci-sriov sysfs enabling mechanism
            sriovfs_path = ("/sys/class/net/%s/device/sriov_numvfs" %
                            get_interface_port_name(context, iface))
            options['pre_up'] = "echo 0 > %s; echo %s > %s" % (
                sriovfs_path, iface['sriov_numvfs'], sriovfs_path)
    elif networktype == constants.NETWORK_TYPE_PCI_PASSTHROUGH:
        sriovfs_path = ("/sys/class/net/%s/device/sriov_numvfs" %
                        get_interface_port_name(context, iface))
        options['pre_up'] = "if [ -f  %s ]; then echo 0 > %s; fi" % (
            sriovfs_path, sriovfs_path)

    config['options'].update(options)
    return config


def get_route_config(route, ifname):
    """
    Builds a basic route config dictionary with all of the fields required to
    format a basic network_route puppet resource.
    """
    if route['prefix']:
        name = '%s/%s' % (route['network'], route['prefix'])
    else:
        name = 'default'
    netmask = IPNetwork(route['network'] + "/" + str(route['prefix'])).netmask
    config = {
        'name': name,
        'ensure': 'present',
        'gateway': route['gateway'],
        'interface': ifname,
        'netmask': str(netmask) if route['prefix'] else '0.0.0.0',
        'network': route['network'] if route['prefix'] else 'default',
        'options': 'metric ' + str(route['metric'])

    }
    return config


def get_common_network_config(context, iface, config):
    """
    Augments a basic config dictionary with the attributes specific to an upper
    layer interface (i.e., an interface that is used to terminate IP traffic).
    """
    traffic_classifier = get_interface_traffic_classifier(context, iface)
    if traffic_classifier:
        config['options']['post_up'] = traffic_classifier

    method = get_interface_address_method(context, iface)
    if method == 'static':
        address = get_interface_primary_address(context, iface)
        if address is None:
            networktype = utils.get_primary_network_type(iface)
            # control interfaces are not required to have an IP address
            if networktype == constants.NETWORK_TYPE_CONTROL:
                return config
            LOG.error("Interface %s has no primary address" % iface['ifname'])
        assert address is not None
        config['ipaddress'] = address['address']
        config['netmask'] = address['netmask']

        gateway = get_interface_gateway_address(context, iface)
        if gateway:
            config['gateway'] = gateway
    return config


def get_interface_network_config(context, iface):
    """
    Builds a network_config resource dictionary for a given interface
    """
    # Create a basic network config resource
    os_ifname = get_interface_os_ifname(context, iface)
    method = get_interface_address_method(context, iface)
    family = get_interface_address_family(context, iface)
    mtu = get_interface_mtu(context, iface)
    config = get_basic_network_config(
        os_ifname, method=method, family=family, mtu=mtu)

    # Add options common to all top level interfaces
    config = get_common_network_config(context, iface, config)

    # Add type specific options
    if iface['iftype'] == constants.INTERFACE_TYPE_VLAN:
        config = get_vlan_network_config(context, iface, config)
    elif iface['iftype'] == constants.INTERFACE_TYPE_AE:
        config = get_bond_network_config(context, iface, config)
    else:
        config = get_ethernet_network_config(context, iface, config)

    return config


def get_bridged_network_config(context, iface):
    """
    Builds a pair of network_config resource dictionaries.  One resource
    represents the actual bridge interface that must be created when bridging a
    physical interface to an avp-provider interface.  The second interface is
    the avp-provider network_config resource.  It is assumed that the physical
    interface network_config resource has already been created by the caller.

    This is the hierarchy:

               "eth0" ->  "br-eth0"  <- "eth0-avp"

    This function creates "eth0-avp" and "br-eth0".
    """
    # Create a config identical to the physical ethernet interface and change
    # the name to the avp-provider interface name.
    avp_config = get_interface_network_config(context, iface)
    avp_config['ifname'] += '-avp'

    # Create a bridge config that ties both interfaces together
    bridge_config = get_bridge_network_config(context, iface)

    return avp_config, bridge_config


def generate_network_config(context, config, iface):
    """
    Produce the puppet network config resources necessary to configure the
    given interface.  In some cases this will emit a single network_config
    resource, while in other cases it will emit multiple resources to create a
    bridge, or to add additional route resources.
    """
    network_config = get_interface_network_config(context, iface)

    config[NETWORK_CONFIG_RESOURCE].update({
        network_config['ifname']: format_network_config(network_config)
    })

    # Add additional configs for special interfaces
    if is_bridged_interface(context, iface):
        avp_config, bridge_config = get_bridged_network_config(context, iface)
        config[NETWORK_CONFIG_RESOURCE].update({
            avp_config['ifname']: format_network_config(avp_config),
            bridge_config['ifname']: format_network_config(bridge_config),
        })

    # Add complementary puppet resource definitions (if needed)
    for route in get_interface_routes(context, iface):
        route_config = get_route_config(route, network_config['ifname'])
        config[ROUTE_CONFIG_RESOURCE].update({
            route_config['name']: route_config
        })


def find_interface_by_type(context, networktype):
    """
    Lookup an interface based on networktype.  This is only intended for
    platform interfaces that have only 1 such interface per node (i.e., oam,
    mgmt, infra, pxeboot, bmc).
    """
    for ifname, iface in six.iteritems(context['interfaces']):
        if networktype == utils.get_primary_network_type(iface):
            return iface


def find_address_by_type(context, networktype):
    """
    Lookup an address based on networktype.  This is only intended for for
    types that only have 1 such address per node.  For example, for SDN we
    only expect/support a single data IP address per node because the SDN
    controller cannot support more than 1.
    """
    for ifname, addresses in six.iteritems(context['addresses']):
        for address in addresses:
            if address['networktype'] == networktype:
                return address['address'], address['prefix']
    return None, None


def find_sriov_interfaces_by_driver(context, driver):
    """
    Lookup all interfaces based on port driver.
    To be noted that this is only used for IFTYPE_ETHERNET
    """
    ifaces = []
    for ifname, iface in six.iteritems(context['interfaces']):
        if iface['iftype'] != constants.INTERFACE_TYPE_ETHERNET:
            continue
        port = get_interface_port(context, iface)
        networktype = utils.get_primary_network_type(iface)
        if (port['driver'] == driver and
                networktype == constants.NETWORK_TYPE_PCI_SRIOV):
            ifaces.append(iface)
    return ifaces


def count_interfaces_by_type(context, networktypes):
    """
    Count the number of interfaces with a matching network type.
    """
    for ifname, iface in six.iteritems(context['interfaces']):
        networktypelist = utils.get_network_type_list(iface)
        if any(n in networktypelist for n in networktypes):
            return iface


def interface_sort_key(iface):
    """
    Sort interfaces by interface type placing ethernet interfaces ahead of
    aggregated ethernet interfaces, and vlan interfaces last.
    """
    if iface['iftype'] == constants.INTERFACE_TYPE_ETHERNET:
        return 0, iface['ifname']
    elif iface['iftype'] == constants.INTERFACE_TYPE_AE:
        return 1, iface['ifname']
    else:  # if iface['iftype'] == constants.INTERFACE_TYPE_VLAN:
        return 2, iface['ifname']


def generate_interface_configs(context, config):
    """
    Generate the puppet resource for each of the interface and route config
    resources.
    """
    for iface in sorted(context['interfaces'].values(),
                        key=interface_sort_key):
        if needs_interface_config(context, iface):
            generate_network_config(context, config, iface)


def get_address_config(context, iface, address):
    ifname = get_interface_os_ifname(context, iface)
    return {
        'ifname': ifname,
        'address': address,
    }


def generate_address_configs(context, config):
    """
    Generate the puppet resource for each of the floating IP addresses
    """
    for networktype, address in six.iteritems(context['floatingips']):
        iface = find_interface_by_type(context, networktype)
        if iface:
            address_config = get_address_config(context, iface, address)
            config[ADDRESS_CONFIG_RESOURCE].update({
                networktype: address_config
            })
        elif networktype == constants.NETWORK_TYPE_PXEBOOT:
            # Fallback PXE boot address against mananagement interface
            iface = find_interface_by_type(context,
                                           constants.NETWORK_TYPE_MGMT)
            if iface:
                address_config = get_address_config(context, iface, address)
                config[ADDRESS_CONFIG_RESOURCE].update({
                    networktype: address_config
                })


def build_mlx4_num_vfs_options(context):
    """
    Generate the manifest fragment that will create mlx4_core
    modprobe conf file in which VF is set and reload the mlx4_core
    kernel module
    """
    ifaces = find_sriov_interfaces_by_driver(context, DRIVER_MLX_CX3)
    if not ifaces:
        return ""

    num_vfs_options = ""
    for iface in ifaces:
        port = get_interface_port(context, iface)
        # For CX3 SR-IOV configuration, we only configure VFs on the 1st port
        # Since two ports share the same PCI address, if the first port has
        # been configured, we need to skip the second port
        if port['pciaddr'] in num_vfs_options:
            continue

        if not num_vfs_options:
            num_vfs_options = "%s-%d;0;0" % (port['pciaddr'],
                                             iface['sriov_numvfs'])
        else:
            num_vfs_options += ",%s-%d;0;0" % (port['pciaddr'],
                                               iface['sriov_numvfs'])

    return num_vfs_options


def generate_mlx4_core_options(context, config):
    """
    Generate the config options that will create mlx4_core modprobe
    conf file in which VF is set and execute mlx4_core_conf.sh in which
    /var/run/.mlx4_cx3_reboot_required is created to indicate a reboot
    is needed for goenable and /etc/modprobe.d/mlx4_sriov.conf is injected
    into initramfs, this way mlx4_core options can be applied after reboot
    """
    num_vfs_options = build_mlx4_num_vfs_options(context)
    if not num_vfs_options:
        return

    mlx4_core_options = "port_type_array=2,2 num_vfs=%s" % num_vfs_options
    config['platform::networking::mlx4_core_options'] = mlx4_core_options


def generate_driver_config(context, config):
    """
    Generate custom configuration for driver specific parameters.
    """
    if is_compute_subfunction(context):
        generate_mlx4_core_options(context, config)


def generate_loopback_config(config):
    """
    Generate the loopback network config resource so that the loopback
    interface is automatically enabled on reboots.
    """
    network_config = get_basic_network_config(LOOPBACK_IFNAME,
                                              method=LOOPBACK_METHOD)
    config[NETWORK_CONFIG_RESOURCE].update({
        LOOPBACK_IFNAME: format_network_config(network_config)
    })


def format_network_config(config):
    """
    Converts a network_config resource dictionary to the equivalent puppet
    resource definition parameters.
    """
    network_config = copy.copy(config)
    del network_config['ifname']
    return network_config


def generate_dhcp_config(context, config):
    """
    Generate the DHCP client configuration.
    """
    if not is_controller(context):
        infra_interface = find_interface_by_type(
            context, constants.NETWORK_TYPE_INFRA)
        if infra_interface:
            infra_cid = utils.get_dhcp_cid(context['hostname'],
                                           constants.NETWORK_TYPE_INFRA,
                                           infra_interface['imac'])
            config['platform::dhclient::params::infra_client_id'] = infra_cid