# coding=utf-8
# Copyright 2016 F5 Networks Inc.
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

import os
from oslo_log import log as logging

LOG = logging.getLogger(__name__)


class SSLProfileError(Exception):
    pass


class SSLProfileHelper(object):

    @staticmethod
    def create_client_ssl_profile(
            bigip, name, cert, key, intermediate=None, sni_default=False, parent_profile=None, caClientTrust=False,
            key_passphrase=None):
        uploader = bigip.shared.file_transfer.uploads
        cert_registrar = bigip.tm.sys.crypto.certs
        intermediate_registrar = bigip.tm.sys.crypto.certs
        key_registrar = bigip.tm.sys.crypto.keys
        ssl_client_profile = bigip.tm.ltm.profile.client_ssls.client_ssl

        profilename = name
        if not sni_default:
            profilename = name + '_NotDefault'

        # No need to create if it exists
        if ssl_client_profile.exists(name=profilename, partition='Common'):
            return

        # Check that parent profile exists; use default if not.
        if parent_profile and not ssl_client_profile.exists(
                name=parent_profile, partition='Common'):
            parent_profile = None

        certfilename = name + '.crt'
        keyfilename = name + '.key'
        # we need both names because uploader fiddles around with names
        intermediatefilename = name + '.chain'
        intermediatecrtfilename = intermediatefilename + '.crt'

        try:
            # In-memory upload -- data not written to local file system but
            # is saved as a file on the BIG-IP.
            uploader.upload_bytes(cert, certfilename)
            uploader.upload_bytes(key, keyfilename)
            if intermediate:
                uploader.upload_bytes(intermediate, intermediatefilename)

            # import certificate
            param_set = {}
            param_set['name'] = certfilename
            param_set['from-local-file'] = os.path.join(
                '/var/config/rest/downloads/', certfilename)
            cert_registrar.exec_cmd('install', **param_set)

            # import key
            param_set['name'] = keyfilename
            param_set['from-local-file'] = os.path.join(
                '/var/config/rest/downloads/', keyfilename)
            key_registrar.exec_cmd('install', **param_set)

            if intermediate:
                # import intermediates
                param_set = {}
                param_set['name'] = intermediatefilename
                param_set['from-local-file'] = os.path.join(
                    '/var/config/rest/downloads/', intermediatefilename)
                intermediate_registrar.exec_cmd('install', **param_set)

            # create ssl-client profile from cert/key pair
            if intermediate:
                chain = [{'name': name,
                      'cert': '/Common/' + certfilename,
                      'key': '/Common/' + keyfilename,
                      'chain': '/Common/' + intermediatecrtfilename}]
            # create ssl-client profile from cert/key pair
            else:
                chain = [{'name': name,
                      'cert': '/Common/' + certfilename,
                      'key': '/Common/' + keyfilename}]

            if key_passphrase:
                chain[0]['passphrase'] = key_passphrase

            if caClientTrust and intermediate:
                ssl_client_profile.create(name=profilename,
                                      partition='Common',
                                      certKeyChain=chain,
                                      sniDefault=sni_default,
                                      defaultsFrom=parent_profile,
                                      clientCertCa=intermediatecrtfilename,
                                      caFile=intermediatecrtfilename)
                LOG.info("Creating SSL profile WITH caClientTrust and WITH intermediate %s", chain)
            elif (not caClientTrust) and intermediate:
                ssl_client_profile.create(name=profilename,
                                      partition='Common',
                                      certKeyChain=chain,
                                      sniDefault=sni_default,
                                      defaultsFrom=parent_profile)
                LOG.info("Creating SSL profile WITHOUT caClientTrust and WITH intermediate %s", chain)
            elif (not caClientTrust) and (not intermediate):
                ssl_client_profile.create(name=profilename,
                                          partition='Common',
                                          certKeyChain=chain,
                                          sniDefault=sni_default,
                                          defaultsFrom=parent_profile)
                LOG.info("Creating SSL profile WITHOUT caClientTrust and WITHOUT intermediate %s", chain)
            else:
                LOG.error("ERROR: Cannot create a SSL profile WITH caClientTrust and WITHOUT intermediate")
                raise SSLProfileError("ERROR: Cannot create a SSL profile WITH caClientTrust and WITHOUT intermediate")
        except Exception as err:
            LOG.error("Error creating SSL profile: %s" % err.message)
            raise SSLProfileError(err.message)

    @staticmethod
    def get_client_ssl_profile_count(bigip):
        return len(
            bigip.tm.ltm.profile.client_ssls.get_collection(
                partition='Common'))
