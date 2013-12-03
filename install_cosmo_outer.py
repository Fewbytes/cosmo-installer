#!/usr/bin/env python
# vim: ts=4 sw=4 et

# Standard
import argparse
import json
import os
import sys


# OpenStack
import keystoneclient.v2_0.client as keystone_client
import neutronclient.neutron.client as neutron_client

EP_FLAG = 'externally_provisioned'

EXTERNAL_PORTS = (9000,)


class OpenStackLogicError(RuntimeError):
    pass


class CreateOrEnsureExists(object):

    def __init__(self, logger):
        self.create_or_ensure_logger = logger

    def create_or_ensure_exists(self, config, *args, **kw):
        # config hash is only used for 'externally_provisioned' attribute
        if 'externally_provisioned' in config and config['externally_provisioned']:
            method = 'ensure_exists'
        else:
            method = 'check_and_create'
        return getattr(self, method)(*args, **kw)

    def check_and_create(self, name, *args, **kw):
        self.create_or_ensure_logger.info("Will create {0} '{1}'".format(self.__class__.WHAT, name))
        if self.find_by_name(name):
            raise OpenStackLogicError("{0} '{1}' already exists".format(self.__class__.WHAT, name))
        return self.create(name, *args, **kw)

    def ensure_exists(self, name, *args, **kw):
        self.create_or_ensure_logger.info("Will use existing {0} '{1}'".format(self.__class__.WHAT, name))
        ret = self.find_by_name(name)
        if not ret:
            raise OpenStackLogicError("{0} '{1}' was not found".format(self.__class__.WHAT, name))
        return ret

    def find_by_name(self, name):
        matches = self.list_objects_with_name(name)

        if len(matches) == 0:
            return None
        if len(matches) == 1:
            return matches[0]['id']
        raise OpenStackLogicError("Lookup of {0} named '{1}' failed. There are {2} matches."
                                  .format(self.__class__.WHAT, name, len(matches)))


class CreateOrEnsureExistsNeutron(CreateOrEnsureExists):

    def __init__(self, logger, connector):
        CreateOrEnsureExists.__init__(self, logger)
        self.neutron_client = connector.get_neutron_client()


class OpenStackNetworkCreator(CreateOrEnsureExistsNeutron):

    WHAT = 'network'

    def list_objects_with_name(self, name):
        return self.neutron_client.list_networks(name=name)['networks']

    def create(self, name, ext=False):
        ret = self.neutron_client.create_network({
            'network': {
                'name': name,
                'admin_state_up': True,
                'router:external': ext
            }
        })
        return ret['network']['id']


class OpenStackSubnetCreator(CreateOrEnsureExistsNeutron):

    WHAT = 'subnet'

    def list_objects_with_name(self, name):
        return self.neutron_client.list_subnets(name=name)['subnets']

    def create(self, name, ip_version, cidr, net_id):
        ret = self.neutron_client.create_subnet({
            'subnet': {
                'name': name,
                'ip_version': ip_version,
                'cidr': cidr,
                'network_id': net_id
            }
        })
        return ret['subnet']['id']


class OpenStackRouterCreator(CreateOrEnsureExistsNeutron):

    WHAT = 'router'

    def list_objects_with_name(self, name):
        return self.neutron_client.list_routers(name=name)['routers']

    def create(self, name, interfaces=None, external_gateway_info=None):
        args = {
            'router': {
                'name': name,
                'admin_state_up': True
            }
        }
        if external_gateway_info:
            args['router']['external_gateway_info'] = external_gateway_info
        router_id = self.neutron_client.create_router(args)['router']['id']
        if interfaces:
            for i in interfaces:
                self.neutron_client.add_interface_router(router_id, i)
        return router_id


class OpenStackSecurityGroupCreator(CreateOrEnsureExistsNeutron):

    WHAT = 'security group'

    def list_objects_with_name(self, name):
        return self.neutron_client.list_security_groups(name=name)['security_groups']

    def create(self, name, ext=False):
        ret = self.neutron_client.create_security_group({
            'security_group': {
                'name': name
            }
        })
        return ret['security_group']['id']


class OpenStackConnector(object):

    # TODO: maybe lazy?
    def __init__(self, config):
        self.config = config
        self.keystone_client = keystone_client.Client(**self.config['keystone'])

        self.neutron_client = neutron_client.Client('2.0', endpoint_url=config['neutron']['url'], token=self.keystone_client.auth_token)
        self.neutron_client.format = 'json'

    def get_keystone_client(self):
        return self.keystone_client

    def get_neutron_client(self):
        return self.neutron_client


class CosmoOnOpenStackInstaller(object):
    """ Installs Cosmo on OpenStack """

    def __init__(self, config, network_creator, subnet_creator, router_creator, sg_creator):
        self.config = config
        self.network_creator = network_creator
        self.subnet_creator = subnet_creator
        self.router_creator = router_creator
        self.sg_creator = sg_creator

    def run(self):

        nconf = self.config['management']['network']
        net_id = self.network_creator.create_or_ensure_exists(nconf, nconf['name'])

        sconf = self.config['management']['subnet']
        subnet_id = self.subnet_creator.create_or_ensure_exists(sconf, sconf['name'], sconf['ip_version'], sconf['cidr'], net_id)

        enconf = self.config['management']['ext_network']
        enet_id = self.network_creator.create_or_ensure_exists(enconf, enconf['name'], ext=True)

        rconf = self.config['management']['router']
        self.router_creator.create_or_ensure_exists(rconf, rconf['name'], interfaces=[
            {'subnet_id': subnet_id},
        ], external_gateway_info={"network_id": enet_id})

        sgconf = self.config['management']['security_group_ext']
        sg_id = self.sg_creator.create_or_ensure_exists(sgconf, sgconf['name'])

if __name__ == '__main__':
    parser = argparse.ArgumentParser('Installs Cosmo in an OpenStack environment')
    parser.add_argument('config_file_path', metavar='CONFIG_FILE')
    args = parser.parse_args()

    with open(args.config_file_path) as f:
        config = json.loads(f.read())

    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    connector = OpenStackConnector(config)
    network_creator = OpenStackNetworkCreator(logger, connector)
    subnet_creator = OpenStackSubnetCreator(logger, connector)
    router_creator = OpenStackRouterCreator(logger, connector)
    sg_creator = OpenStackSecurityGroupCreator(logger, connector)
    installer = CosmoOnOpenStackInstaller(config, network_creator, subnet_creator, router_creator, sg_creator)
    installer.run()
