#!/usr/bin/python

import time
from twisted.internet import defer, reactor, task
from twisted.python.filepath import FilePath

from lae_automation.config import Config
from lae_automation.initialize import activate_user_account_desktop, verify_user_account, \
    create_user_bucket, deploy_EC2_instance
from lae_automation.aws.queryapi import wait_for_EC2_properties, AddressParser, TimeoutError
from lae_automation.server import install_server, bounce_server, notify_zenoss, NotListeningError
from lae_automation.confirmation import send_signup_confirmation, send_notify_failure
from lae_util.servers import append_record


EC2_ENDPOINT = 'https://ec2.us-east-1.amazonaws.com/'
#EC2_ENDPOINT = 'https://ec2.amazonaws.com/'

POLL_TIME = 30

# credit card verification might take 15 minutes, so wait 20.
CC_VERIFICATION_TIME = 20 * 60

# wait 20 seconds before the first poll, then up to 5 minutes for the addresses.
ADDRESS_DELAY_TIME = 20
ADDRESS_WAIT_TIME = 5 * 60

LISTEN_RETRIES = 5
LISTEN_POLL_TIME = 15


def wait_for_EC2_addresses(ec2accesskeyid, ec2secretkey, endpoint_uri, stdout, stderr, *instance_ids):
    return wait_for_EC2_properties(ec2accesskeyid, ec2secretkey, endpoint_uri, AddressParser(),
                                   POLL_TIME, ADDRESS_WAIT_TIME, stdout, stderr, *instance_ids)


def lookup_product(config, productcode):
    ps = [p for p in config.products if p['product_code'] == productcode]
    if len(ps) != 1:
        raise AssertionError("Product code %r matches %d products." % (productcode, len(ps)))

    return ps[0]


def signup(activationkey, productcode, customer_name, customer_email, customer_keyinfo, stdout, stderr,
           seed, secretsfile, logfilename, configpath='../lae_automation_config.json',
           serverinfopath=None, ec2secretpath=None, clock=None):
    config = Config(configpath)
    myclock = clock or reactor

    bucketname = "lae-%s-%s" % (productcode.lower(), seed)
    location = None  # default location for now
    product = lookup_product(config, productcode)
    fullname = product['full_name']
    producttoken = product['product_token']
    amiimageid = product['ami_image_id']
    instancesize = product['instance_size']

    print >>stdout, "Signing up customer for %s..." % (fullname,)

    d = activate_user_account_desktop(activationkey, producttoken, stdout, stderr)
    def _activated(adpr):
        useraccesskeyid = adpr.access_key_id
        usersecretkey = adpr.secret_key
        usertoken = adpr.usertoken

        def _wait_until_verified(how_long_secs):
            d3 = verify_user_account(useraccesskeyid, usersecretkey, usertoken, producttoken, stdout, stderr)
            def _maybe_again(res):
                if res:
                    print >>stdout, "Subscription verified."
                    return
                if how_long_secs <= 0.0:
                    print >>stdout, "Timed out waiting for verification of subscription."
                    raise TimeoutError()
                print >>stdout, "Waiting another %d seconds..." % (POLL_TIME,)
                return task.deferLater(myclock, POLL_TIME, _wait_until_verified, how_long_secs - POLL_TIME)
            d3.addCallback(_maybe_again)
            return d3

        d2 = _wait_until_verified(CC_VERIFICATION_TIME)

        d2.addCallback(lambda ign: create_user_bucket(useraccesskeyid, usersecretkey, usertoken, bucketname, stdout, stderr,
                                                      producttoken=producttoken, location=location))

        # We could deploy and configure the instance in parallel with the above wait and delete it
        # if necessary, but let's keep it simple and sequential.
        d2.addCallback(lambda ign: deploy_server(useraccesskeyid, usersecretkey, usertoken, producttoken,
                                                 bucketname, None, amiimageid, instancesize,
                                                 customer_name, customer_email, customer_keyinfo, stdout, stderr,
                                                 secretsfile, config, serverinfopath, ec2secretpath, clock=myclock))
        return d2
    d.addCallback(_activated)
    d.addErrback(lambda f: send_notify_failure(f, customer_name, customer_email, logfilename, stdout, stderr))
    return d


def replace_server(oldsecrets, amiimageid, instancesize, customer_email, stdout, stderr,
                   secretsfile, logfilename, configpath='../lae_automation_config.json',
                   serverinfopath=None, ec2secretpath=None, clock=None):
    config = Config(configpath)
    useraccesskeyid = oldsecrets['access_key_id']
    usersecretkey   = oldsecrets['secret_key']
    usertoken       = oldsecrets['user_token']
    producttoken    = oldsecrets['product_token']
    bucketname      = oldsecrets["bucket_name"]

    d = deploy_server(useraccesskeyid, usersecretkey, usertoken, producttoken,
                      bucketname, oldsecrets, amiimageid, instancesize,
                      "someone", customer_email, None, stdout, stderr,
                      secretsfile, config, serverinfopath, ec2secretpath, clock)
    d.addErrback(lambda f: send_notify_failure(f, "someone", customer_email, logfilename, stdout, stderr))
    return d


# TODO: too many args. Consider passing them in a dict.
def deploy_server(useraccesskeyid, usersecretkey, usertoken, producttoken,
                  bucketname, oldsecrets, amiimageid, instancesize,
                  customer_name, customer_email, customer_keyinfo, stdout, stderr,
                  secretsfile, config, serverinfopath=None, ec2secretpath=None, clock=None):
    serverinfopath = serverinfopath or '../serverinfo.csv'
    ec2secretpath = ec2secretpath or '../ec2secret'
    myclock = clock or reactor

    ec2accesskeyid = str(config.other['ec2_access_key_id'])
    ec2secretkey = FilePath(ec2secretpath).getContent().strip()

    instancename = customer_email  # need not be unique

    admin_keypair_name = str(config.other['admin_keypair_name'])
    admin_privkey_path = str(config.other['admin_privkey_path'])
    monitor_pubkey = FilePath(str(config.other['monitor_pubkey_path'])).getContent().strip()
    monitor_privkey_path = str(config.other['monitor_privkey_path'])
    zenoss_privkey_path = str(config.other['zenoss_privkey_path'])
    zenoss_IP = str(config.other['zenoss_IP'])

    d = deploy_EC2_instance(ec2accesskeyid, ec2secretkey, EC2_ENDPOINT, amiimageid,
                            instancesize, bucketname, admin_keypair_name, instancename,
                            stdout, stderr)

    def _deployed(instance):
        d2 = task.deferLater(myclock, ADDRESS_DELAY_TIME, wait_for_EC2_addresses,
                             ec2accesskeyid, ec2secretkey, EC2_ENDPOINT, stdout, stderr,
                             instance.instance_id)

        def _got_addresses(addresses):
            assert len(addresses) == 1, addresses
            (publichost, privatehost) = addresses[0]
            print >>stdout, "The server's public address is %r." % (publichost,)

            retries = LISTEN_RETRIES
            while True:
                try:
                    install_server(publichost, admin_privkey_path, monitor_pubkey, monitor_privkey_path, stdout, stderr)
                    break
                except NotListeningError:
                    retries -= 1
                    if retries <= 0:
                        print >>stdout, "Timed out waiting for EC2 instance to listen for ssh connections."
                        raise TimeoutError()
                    print >>stdout, "Waiting another %d seconds..." % (LISTEN_POLL_TIME)
                    time.sleep(LISTEN_POLL_TIME)
                    continue

            furl = bounce_server(publichost, admin_privkey_path, privatehost, useraccesskeyid, usersecretkey, usertoken,
                                 producttoken, bucketname, oldsecrets, stdout, stderr, secretsfile)

            append_record(FilePath(serverinfopath), instance.launch_time, instance.instance_id, publichost)

            d3 = defer.succeed(None)
            if not oldsecrets:
                d3.addCallback(lambda ign: send_signup_confirmation(publichost, customer_name, customer_email, furl, customer_keyinfo,
                                                                    stdout, stderr))
            def _setup_monitoring(ign):
                print >>stdout, "Setting up monitoring..."
                notify_zenoss(publichost, zenoss_IP, zenoss_privkey_path)
            d3.addCallback(_setup_monitoring)
            return d3
        d2.addCallback(_got_addresses)
        return d2
    d.addCallback(_deployed)
    return d
