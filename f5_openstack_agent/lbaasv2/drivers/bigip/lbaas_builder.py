# coding=utf-8
# Copyright 2014-2017 F5 Networks Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from time import time

from oslo_log import log as logging

from neutron.plugins.common import constants as plugin_const
from neutron_lbaas.services.loadbalancer import constants as lb_const

from f5_openstack_agent.lbaasv2.drivers.bigip import exceptions as f5_ex
from f5_openstack_agent.lbaasv2.drivers.bigip import l7policy_service
from f5_openstack_agent.lbaasv2.drivers.bigip import listener_service
from f5_openstack_agent.lbaasv2.drivers.bigip import pool_service
from f5_openstack_agent.lbaasv2.drivers.bigip import virtual_address

from f5_openstack_agent.lbaasv2.drivers.bigip import utils

from requests import HTTPError

#import pdb

LOG = logging.getLogger(__name__)


class LBaaSBuilder(object):
    # F5 LBaaS Driver using iControl for BIG-IP to
    # create objects (vips, pools) - not using an iApp."""

    def __init__(self, conf, driver, l2_service=None):
        self.conf = conf
        self.driver = driver
        self.l2_service = l2_service
        self.service_adapter = driver.service_adapter
        self.listener_builder = listener_service.ListenerServiceBuilder(self,
                                                                        self.service_adapter,
                                                                        driver.cert_manager,
                                                                        conf.f5_parent_ssl_profile)
        self.pool_builder = pool_service.PoolServiceBuilder(
            self.service_adapter,
            conf.f5_parent_https_monitor)
        self.l7service = l7policy_service.L7PolicyService(self, conf)
        self.esd = None

    @utils.instrument_execution_time
    def assure_service(self, service, traffic_group, all_subnet_hints, delete_event=False):
        """Assure that a service is configured on the BIGIP."""
        start_time = time()
        LOG.debug("Starting assure_service")

        # Needed also for delete events because of subnet hints
        self._assure_loadbalancer_created(service, all_subnet_hints)
        # Create and update
        if not delete_event:
            self._assure_pools_created(service)

            self._assure_listeners_created(service)

            self._assure_monitors_created(service)

            self._assure_members_created(service, all_subnet_hints)

            self._assure_pools_configured(service)

            self._assure_l7policies_created(service)

            self._assure_l7rules_created(service)
        else: # delete
            self._assure_monitors_deleted(service)

            self._assure_members_deleted(service, all_subnet_hints)

            self._assure_l7rules_deleted(service)

            self._assure_l7policies_deleted(service)

            self._assure_pools_deleted(service)

            self._assure_listeners_deleted(service)

            self._assure_loadbalancer_deleted(service)

        LOG.debug("    _assure_service took %.5f secs" %
                  (time() - start_time))
        return all_subnet_hints

    @utils.instrument_execution_time
    def _assure_loadbalancer_created(self, service, all_subnet_hints):
        if 'loadbalancer' not in service:
            return
        bigips = self.driver.get_config_bigips()
        loadbalancer = service["loadbalancer"]

        vip_address = virtual_address.VirtualAddress(
            self.service_adapter,
            loadbalancer)
        for bigip in bigips:
            vip_address.assure(bigip)

        if self.driver.l3_binding:
            loadbalancer = service["loadbalancer"]
            self.driver.l3_binding.bind_address(
                subnet_id=loadbalancer["vip_subnet_id"],
                ip_address=loadbalancer["vip_address"])

        self._update_subnet_hints(loadbalancer["provisioning_status"],
                                  loadbalancer["vip_subnet_id"],
                                  loadbalancer["network_id"],
                                  all_subnet_hints,
                                  False)
        if loadbalancer['provisioning_status'] != plugin_const.PENDING_DELETE:
            loadbalancer['provisioning_status'] = plugin_const.ACTIVE

    @utils.instrument_execution_time
    def _assure_listeners_created(self, service):
        if 'listeners' not in service:
            return

        listeners = service["listeners"]
        loadbalancer = service["loadbalancer"]
        networks = service["networks"]
        bigips = self.driver.get_config_bigips()
        old_listener = service.get('old_listener')
        for listener in listeners:
            if (old_listener != None and old_listener.get('id') == listener.get('id')):
                svc = {"loadbalancer": loadbalancer,
                       "listener": listener,
                       "old_listener": old_listener,
                       "networks": networks}
            else:
                svc = {"loadbalancer": loadbalancer,
                       "listener": listener,
                       "networks": networks}

            default_pool_id = listener.get('default_pool_id', '')
            if default_pool_id:
                pool = self.get_pool_by_id(service, default_pool_id)
                if pool:
                    svc['pool'] = pool

            if listener['provisioning_status'] == plugin_const.PENDING_UPDATE:
                try:
                    self.listener_builder.update_listener(svc, bigips)
                except Exception as err:
                    loadbalancer['provisioning_status'] = plugin_const.ERROR
                    listener['provisioning_status'] = plugin_const.ERROR
                    LOG.exception(err)
                    raise f5_ex.VirtualServerUpdateException(err.message)

            elif listener['provisioning_status'] != \
                    plugin_const.PENDING_DELETE:
                try:
                    # create_listener() will do an update if VS exists
                    self.listener_builder.create_listener(svc, bigips)
                    listener['operating_status'] = \
                        svc['listener']['operating_status']
                except Exception as err:
                    loadbalancer['provisioning_status'] = plugin_const.ERROR
                    listener['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.VirtualServerCreationException(err.message)

            if listener['provisioning_status'] != plugin_const.PENDING_DELETE:
                listener['provisioning_status'] = plugin_const.ACTIVE

    @utils.instrument_execution_time
    def _assure_pools_created(self, service):
        if "pools" not in service:
            return

        pools = service["pools"]
        loadbalancer = service["loadbalancer"]

        bigips = self.driver.get_config_bigips()

        for pool in pools:
            if pool['provisioning_status'] != plugin_const.PENDING_DELETE:
                svc = {"loadbalancer": loadbalancer, "pool": pool}
                svc['members'] = self._get_pool_members(service, pool['id'])

                try:
                    # create or update pool
                    if pool['provisioning_status'] == \
                            plugin_const.PENDING_CREATE:
                        self.pool_builder.create_pool(svc, bigips)
                    else:
                        try:
                            self.pool_builder.update_pool(svc, bigips)
                        except HTTPError as err:
                            if err.response.status_code == 404:
                                self.pool_builder.create_pool(svc, bigips)

                except HTTPError as err:
                    if err.response.status_code != 409:
                        pool['provisioning_status'] = plugin_const.ERROR
                        loadbalancer['provisioning_status'] = (
                            plugin_const.ERROR)
                        raise f5_ex.PoolCreationException(err.message)

                except Exception as err:
                    pool['provisioning_status'] = plugin_const.ERROR
                    loadbalancer['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.PoolCreationException(err.message)

                pool['provisioning_status'] = plugin_const.ACTIVE

    @utils.instrument_execution_time
    def _assure_pools_configured(self, service):
        if "pools" not in service:
            return

        pools = service["pools"]
        loadbalancer = service["loadbalancer"]

        bigips = self.driver.get_config_bigips()

        for pool in pools:
            if pool['provisioning_status'] != plugin_const.PENDING_DELETE:
                svc = {"loadbalancer": loadbalancer, "pool": pool}
                svc['members'] = self._get_pool_members(service, pool['id'])

                try:
                    # assign pool name to virtual
                    pool_name = self.service_adapter.init_pool_name(
                        loadbalancer, pool)

                    # get associated listeners for pool
                    for listener in pool['listeners']:
                        listener = self.get_listener_by_id(service, listener['id'])

                        if listener:
                            svc['listener'] = listener
                            self.listener_builder.update_listener_pool(
                                svc, pool_name["name"], bigips)
                            # update virtual sever pool name, session persistence
                            self.listener_builder.update_session_persistence(
                                svc, bigips)

                    # ccloud: update pool to set lb_method right
                    self.pool_builder.update_pool(svc, bigips)

                    pool['provisioning_status'] = plugin_const.ACTIVE

                except HTTPError as err:
                    if err.response.status_code != 409:
                        pool['provisioning_status'] = plugin_const.ERROR
                        loadbalancer['provisioning_status'] = (
                            plugin_const.ERROR)
                        LOG.exception(err)
                        raise f5_ex.PoolCreationException("ccloud: Error #1" + err.message)

                except Exception as err:
                    pool['provisioning_status'] = plugin_const.ERROR
                    loadbalancer['provisioning_status'] = plugin_const.ERROR
                    LOG.exception(err)
                    raise f5_ex.PoolCreationException("ccloud: Error #2" + err.message)

    @utils.instrument_execution_time
    def _get_pool_members(self, service, pool_id):
        '''Return a list of members associated with given pool.'''

        members = []
        for member in service['members']:
            if member['pool_id'] == pool_id:
                members.append(member)
        return members

    @utils.instrument_execution_time
    def _update_listener_pool(self, service, listener_id, pool_name, bigips):
        listener = self.get_listener_by_id(service, listener_id)
        if listener is not None:
            try:
                listener["pool"] = pool_name
                svc = {"loadbalancer": service["loadbalancer"],
                       "listener": listener}
                self.listener_builder.update_listener(svc, bigips)

            except Exception as err:
                listener['provisioning_status'] = plugin_const.ERROR
                raise f5_ex.VirtualServerUpdateException(err.message)

    @utils.instrument_execution_time
    def _assure_monitors_deleted(self, service):

        if not (("pools" in service) and ("healthmonitors" in service)):
            return

        monitors = service["healthmonitors"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        for monitor in monitors:
            svc = {"loadbalancer": loadbalancer,
                   "healthmonitor": monitor,
                   "pool": self.get_pool_by_id(service, monitor["pool_id"])}
            if monitor['provisioning_status'] == plugin_const.PENDING_DELETE:
                try:
                    self.pool_builder.delete_healthmonitor(svc, bigips)
                except Exception as err:
                    monitor['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.MonitorDeleteException(err.message)

    @utils.instrument_execution_time
    def _assure_monitors_created(self, service):

        if not (("pools" in service) and ("healthmonitors" in service)):
            return

        monitors = service["healthmonitors"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        for monitor in monitors:
            svc = {"loadbalancer": loadbalancer,
                   "healthmonitor": monitor,
                   "pool": self.get_pool_by_id(service, monitor["pool_id"])}
            if monitor['provisioning_status'] != plugin_const.PENDING_DELETE:
                try:
                    self.pool_builder.create_healthmonitor(svc, bigips)
                except HTTPError as err:
                    if err.response.status_code != 409:
                        # pool['provisioning_status'] = plugin_const.ERROR
                        loadbalancer['provisioning_status'] = (
                            plugin_const.ERROR
                        )
                        raise f5_ex.MonitorCreationException(err.message)
                    else:
                        self.pool_builder.update_healthmonitor(svc, bigips)
                except Exception as err:
                    monitor['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.MonitorCreationException(err.message)

                monitor['provisioning_status'] = plugin_const.ACTIVE

    @utils.instrument_execution_time
    def _assure_members_created(self, service, all_subnet_hints):
        if not (("pools" in service) and ("members" in service)):
            return

        members = service["members"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        for member in members:
            pool = self.get_pool_by_id(service, member["pool_id"])
            svc = {"loadbalancer": loadbalancer,
                   "member": member,
                   "pool": pool}

            if 'port' not in member and \
                member['provisioning_status'] != plugin_const.PENDING_DELETE:
                LOG.warning("Member definition does not include Neutron port")

            # delete member if pool is being deleted
            if not (member['provisioning_status'] == plugin_const.PENDING_DELETE or \
                pool['provisioning_status'] == plugin_const.PENDING_DELETE):
                try:
                    self.pool_builder.create_member(svc, bigips)
                    member['provisioning_status'] = plugin_const.ACTIVE
                except HTTPError as err:
                    if err.response.status_code != 409:
                        # FIXME(RB)
                        # pool['provisioning_status'] = plugin_const.ERROR
                        loadbalancer['provisioning_status'] = (
                            plugin_const.ERROR
                        )
                        raise f5_ex.MemberCreationException(err.message)
                    else:
                        try:
                            self.pool_builder.update_member(svc, bigips)
                        except Exception as err:
                            member['provisioning_status'] = plugin_const.ERROR
                            raise f5_ex.MemberUpdateException(err.message)


                except Exception as err:
                    member['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.MemberCreationException(err.message)

                self._update_subnet_hints(member["provisioning_status"],
                                          member["subnet_id"],
                                          member["network_id"],
                                          all_subnet_hints,
                                          True)

    @utils.instrument_execution_time
    def _assure_members_deleted(self, service, all_subnet_hints):
        if not (("pools" in service) and ("members" in service)):
            return

        members = service["members"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        for member in members:
            pool = self.get_pool_by_id(service, member["pool_id"])
            svc = {"loadbalancer": loadbalancer,
                   "member": member,
                   "pool": pool}

            if 'port' not in member and \
                member['provisioning_status'] != plugin_const.PENDING_DELETE:
                LOG.warning("Member definition does not include Neutron port")

            # delete member if pool is being deleted
            if member['provisioning_status'] == plugin_const.PENDING_DELETE or \
                pool['provisioning_status'] == plugin_const.PENDING_DELETE:
                try:
                    self.pool_builder.delete_member(svc, bigips)
                except Exception as err:
                    member['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.MemberDeleteException(err.message)

                self._update_subnet_hints(member["provisioning_status"],
                                          member["subnet_id"],
                                          member["network_id"],
                                          all_subnet_hints,
                                          True)


    @utils.instrument_execution_time
    def _assure_loadbalancer_deleted(self, service):
        if (service['loadbalancer']['provisioning_status'] !=
                plugin_const.PENDING_DELETE):
            return

        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        if self.driver.l3_binding:
            self.driver.l3_binding.unbind_address(
                subnet_id=loadbalancer["vip_subnet_id"],
                ip_address=loadbalancer["vip_address"])

        vip_address = virtual_address.VirtualAddress(
            self.service_adapter,
            loadbalancer)

        for bigip in bigips:
            vip_address.assure(bigip, delete=True)

    @utils.instrument_execution_time
    def _assure_pools_deleted(self, service):
        if 'pools' not in service:
            return

        pools = service["pools"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        for pool in pools:
            # Is the pool being deleted?
            if pool['provisioning_status'] == plugin_const.PENDING_DELETE:
                svc = {"loadbalancer": loadbalancer,
                       "pool": pool}

                try:
                    # update listeners for pool
                    for listener in pool['listeners']:
                        svc['listener'] = \
                            self.get_listener_by_id(service, listener['id'])

                        # remove pool name from virtual before deleting pool
                        self.listener_builder.update_listener_pool(
                            svc, "", bigips)

                        self.listener_builder.remove_session_persistence(
                            svc, bigips)

                    # delete pool
                    self.pool_builder.delete_pool(svc, bigips)

                except Exception as err:
                    pool['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.PoolDeleteException(err.message)

    @utils.instrument_execution_time
    def _assure_listeners_deleted(self, service):
        if 'listeners' not in service:
            return

        listeners = service["listeners"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        for listener in listeners:
            if listener['provisioning_status'] == plugin_const.PENDING_DELETE:
                svc = {"loadbalancer": loadbalancer,
                       "listener": listener}
                # ccloud: try to delete persistence which might be attached to listener
                # ignore errors, persistence might be used somewhere else if pool is used more than once as default
                try:
                    self.listener_builder.remove_session_persistence(
                        svc, bigips)
                except Exception:
                    pass
                # delete the listener
                try:
                    self.listener_builder.delete_listener(svc, bigips)
                except Exception as err:
                    listener['provisioning_status'] = plugin_const.ERROR
                    raise f5_ex.VirtualServerDeleteException(err.message)

    @staticmethod
    def _check_monitor_delete(service):
        # If the pool is being deleted, then delete related objects
        if service['pool']['status'] == plugin_const.PENDING_DELETE:
            # Everything needs to be go with the pool, so overwrite
            # service state to appropriately remove all elements
            service['vip']['status'] = plugin_const.PENDING_DELETE
            for member in service['members']:
                member['status'] = plugin_const.PENDING_DELETE
            for monitor in service['pool']['health_monitors_status']:
                monitor['status'] = plugin_const.PENDING_DELETE

    @staticmethod
    def get_pool_by_id(service, pool_id):
        if pool_id and "pools" in service:
            pools = service["pools"]
            for pool in pools:
                if pool["id"] == pool_id:
                    return pool
        return None

    @staticmethod
    def get_listener_by_id(service, listener_id):
        if "listeners" in service:
            listeners = service["listeners"]
            for listener in listeners:
                if listener["id"] == listener_id:
                    return listener
        return None

    def _update_subnet_hints(self, status, subnet_id,
                             network_id, all_subnet_hints, is_member):
        bigips = self.driver.get_config_bigips()
        for bigip in bigips:
            subnet_hints = all_subnet_hints[bigip.device_name]

            if status == plugin_const.PENDING_CREATE or \
                    status == plugin_const.PENDING_UPDATE:
                if subnet_id in subnet_hints['check_for_delete_subnets']:
                    del subnet_hints['check_for_delete_subnets'][subnet_id]
                if subnet_id not in subnet_hints['do_not_delete_subnets']:
                    subnet_hints['do_not_delete_subnets'].append(subnet_id)

            elif status == plugin_const.PENDING_DELETE:
                if subnet_id not in subnet_hints['do_not_delete_subnets']:
                    subnet_hints['check_for_delete_subnets'][subnet_id] = \
                        {'network_id': network_id,
                         'subnet_id': subnet_id,
                         'is_for_member': is_member}

    @utils.instrument_execution_time
    def listener_exists(self, bigip, service):
        """Test the existence of the listener defined by service."""
        try:
            # Throw an exception if the listener does not exist.
            self.listener_builder.get_listener(service, bigip)
        except HTTPError as err:
            LOG.debug("Virtual service service discovery error, %s." %
                      err.message)
            return False

        return True

    @utils.instrument_execution_time
    def _assure_l7policies_created(self, service):
        if 'l7policies' not in service:
            return

        bigips = self.driver.get_config_bigips()
        l7policies = service['l7policies']
        for l7policy in l7policies:
            if l7policy['provisioning_status'] != plugin_const.PENDING_DELETE:
                try:
                    name = l7policy.get('name', None)
                    if name and self.is_esd(name):
                        continue
                    else:
                        self.l7service.create_l7policy(
                            l7policy, service, bigips)
                except Exception as err:
                    l7policy['provisioning_status'] = plugin_const.ERROR
                    service['loadbalancer']['provisioning_status'] = \
                        plugin_const.ERROR
                    raise f5_ex.L7PolicyCreationException(err.message)

                l7policy['provisioning_status'] = plugin_const.ACTIVE

    @utils.instrument_execution_time
    def _assure_l7policies_deleted(self, service):
        if 'l7policies' not in service:
            return

        bigips = self.driver.get_config_bigips()
        l7policies = service['l7policies']
        for l7policy in l7policies:
            if l7policy['provisioning_status'] == plugin_const.PENDING_DELETE:
                try:
                    name = l7policy.get('name', None)
                    if name and self.is_esd(name):
                        continue
                    else:
                        # Note: use update_l7policy because a listener can have
                        # multiple policies
                        self.l7service.update_l7policy(
                            l7policy, service, bigips)
                except Exception as err:
                    l7policy['provisioning_status'] = plugin_const.ERROR
                    service['loadbalancer']['provisioning_status'] = \
                        plugin_const.ERROR
                    raise f5_ex.L7PolicyDeleteException(err.message)

    @utils.instrument_execution_time
    def _assure_l7rules_created(self, service):
        if 'l7policy_rules' not in service:
            return

        bigips = self.driver.get_config_bigips()
        l7rules = service['l7policy_rules']
        for l7rule in l7rules:
            if l7rule['provisioning_status'] != plugin_const.PENDING_DELETE:
                try:
                    # ignore L7 rule if its policy is really an ESD
                    l7policy = self.get_l7policy_for_rule(
                        service['l7policies'], l7rule)
                    name = l7policy.get('name', None)
                    if name and self.is_esd(name):
                        LOG.error("L7 policy {0} is an ESD. Cannot add "
                                  "an L7 rule to and ESD.".format(name))
                        continue

                    self.l7service.create_l7rule(l7rule, service, bigips)
                except Exception as err:
                    l7rule['provisioning_status'] = plugin_const.ERROR
                    service['loadbalancer']['provisioning_status'] = \
                        plugin_const.ERROR
                    raise f5_ex.L7PolicyCreationException(err.message)

                l7rule['provisioning_status'] = plugin_const.ACTIVE
    @utils.instrument_execution_time
    def _assure_l7rules_deleted(self, service):
        if 'l7policy_rules' not in service:
            return

        bigips = self.driver.get_config_bigips()
        l7rules = service['l7policy_rules']
        for l7rule in l7rules:
            if l7rule['provisioning_status'] == plugin_const.PENDING_DELETE:
                try:
                    # ignore L7 rule if its policy is really an ESD
                    l7policy = self.get_l7policy_for_rule(
                        service['l7policies'], l7rule)
                    name = l7policy.get('name', None)
                    if name and self.is_esd(name):
                        continue
                    self.l7service.bigips = self.driver.get_config_bigips()
                    self.l7service.delete_l7rule(l7rule, service, bigips)
                except Exception as err:
                    l7rule['provisioning_status'] = plugin_const.ERROR
                    service['loadbalancer']['provisioning_status'] = \
                        plugin_const.ERROR
                    raise f5_ex.L7PolicyDeleteException(err.message)
    @utils.instrument_execution_time
    def get_listener_stats(self, service, stats):
        """Get statistics for a loadbalancer service.

        Sums values for stats defined in stats dictionary for all listeners
        defined in service object. For example, if loadbalancer has two
        listeners and stats defines a stat 'clientside.bitsIn' as a key, the
        sum of all pools' clientside.bitsIn will be returned in stats.

        Provisioning status is ignored -- PENDING_DELETE objects are
        included.

        :param service: defines loadbalancer and set of pools.
        :param stats: a dictionary that defines which stats to get.
        Should be initialized by caller with 0 values.
        :return: stats are appended to input stats dict (i.e., contains
        the sum of given stats for all BIG-IPs).
        """

        listeners = service["listeners"]
        loadbalancer = service["loadbalancer"]
        bigips = self.driver.get_config_bigips()

        collected_stats = {}
        for stat in stats:
            collected_stats[stat] = 0

        for listener in listeners:
            svc = {"loadbalancer": loadbalancer, "listener": listener}
            vs_stats = self.listener_builder.get_stats(svc, bigips, stats)
            for stat in stats:
                collected_stats[stat] += vs_stats[stat]

        return collected_stats
    @utils.instrument_execution_time
    def update_operating_status(self, service):
        bigip = self.driver.get_active_bigip()
        loadbalancer = service["loadbalancer"]
        status_keys = ['status.availabilityState',
                       'status.enabledState']

        members = service["members"]
        for member in members:
            if member['provisioning_status'] == plugin_const.ACTIVE:
                pool = self.get_pool_by_id(service, member["pool_id"])
                svc = {"loadbalancer": loadbalancer,
                       "member": member,
                       "pool": pool}
                status = self.pool_builder.get_member_status(
                    svc, bigip, status_keys)
                member['operating_status'] = self.convert_operating_status(
                    status)

    @staticmethod
    def convert_operating_status(status):
        """Convert object status to LBaaS operating status.

        status.availabilityState and  status.enabledState = Operating Status

        available                     enabled                 ONLINE
        available                     disabled                DISABLED
        offline                       -                       OFFLINE
        unknown                       -                       NO_MONITOR
        """
        op_status = None
        available = status.get('status.availabilityState', '')
        if available == 'available':
            enabled = status.get('status.enabledState', '')
            if enabled == 'enabled':
                op_status = lb_const.ONLINE
            elif enabled == 'disabled':
                op_status = lb_const.DISABLED
            else:
                LOG.warning('Unexpected value %s for status.enabledState',
                            enabled)
        elif available == 'offline':
            op_status = lb_const.OFFLINE
        elif available == 'unknown':
            op_status = lb_const.NO_MONITOR

        return op_status

    def get_l7policy_for_rule(self, l7policies, l7rule):
        policy_id = l7rule['policy_id']
        for policy in l7policies:
            if policy_id == policy['id']:
                return policy

        return None

    def init_esd(self, esd):
        self.esd = esd

    def get_esd(self, name):
        if self.esd:
            return self.esd.get_esd(name)

        return None

    def is_esd(self, name):
        return self.esd.get_esd(name) is not None