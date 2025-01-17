# *********************************************************************
# Deep Security - Is Instance Protected By _______?
# *********************************************************************

# Standard library
import datetime
import json
import distutils.util

# Project libraries
from src.deepsecurity.dsm import Manager
from src.deepsecurity.credentials import Credentials

# 3rd party libraries
import boto3


def aws_config_rule_handler(event, context):
    """
    Primary entry point for the AWS Lambda function

    Verify whether or not the specified instance is protected by a specific
    Deep Security control

    print() statments are for the benefit of CloudWatch logs & a nod to old school
    debugging ;-)
    """
    instance_id = None
    is_protected = False
    detailed_msg = ""

    # Make sure the function has been called in the context of AWS Config Rules
    if 'invokingEvent' not in event or \
       'ruleParameters' not in event or \
       'resultToken' not in event or \
       'eventLeftScope' not in event:
       print("Missing a required AWS Config Rules key in the event object. Need [invokingEvent, ruleParameters, resultToken, eventLeftScope]")
       return { 'result': 'error' }

    # Convert any test events to json (only needed for direct testing through the AWS Lambda Management Console)
    if 'ruleParameters' in event and not type(event['ruleParameters']) == type({}): event['ruleParameters'] = json.loads(event['ruleParameters'])
    if 'invokingEvent' in event and not type(event['invokingEvent']) == type({}): event['invokingEvent'] = json.loads(event['invokingEvent'])

    # Make sure we have the required rule parameters
    if 'ruleParameters' in event:
        if 'dsUsernameKey' not in event['ruleParameters'] and \
             'dsPasswordKey' not in event['ruleParameters'] and \
             ('dsTenant' not in event['ruleParameters'] or 'dsHostname' not in event['ruleParameters']):
            return { 'requirements_not_met': 'Function requires that you at least pass dsUsernameKey, dsPasswordKey, and either dsTenant or dsHostname'}
        else:
            print("Credentials for Deep Security passed to function successfully")
            credentials = Credentials(event['ruleParameters']['dsUsernameKey'], event['ruleParameters']['dsPasswordKey'])
            ds_username = credentials.get_username()
            ds_password = credentials.get_password()

        if 'dsControl' not in event['ruleParameters'] or \
            not event['ruleParameters']['dsControl'].lower() in [ 'anti_malware', 'web_reputation', 'firewall', 'intrusion_prevention', 'integrity_monitoring', 'log_inspection' ]:
            return { 'requirements_not_met': 'Function requires that you specify the desired Deep Security control to verify. Valid choices are [ anti_malware, web_reputation, firewall, intrusion_prevention, integrity_monitoring, log_inspection ]' }

    # Determine if this is an EC2 instance event
    if 'invokingEvent' in event:
        if 'configurationItem' in event['invokingEvent']:
            if 'resourceType' in event['invokingEvent']['configurationItem'] and event['invokingEvent']['configurationItem']['resourceType'].lower() == "AWS::EC2::Instance".lower():
                # Something happened to an EC2 instance, we don't worry about what happened
                # the fact that something did is enough to trigger a re-check
                instance_id = event['invokingEvent']['configurationItem']['resourceId'] if 'resourceId' in event['invokingEvent']['configurationItem'] else None
                if instance_id: print("Target instance [{}]".format(instance_id))
            else:
                print("Event is not targeted towards a resourceType of AWS::EC2::Instance")

    if instance_id:
        # We know this instance ID was somehow impacted, check it's status in Deep Security
        ds_tenant = event['ruleParameters']['dsTenant'] if 'dsTenant' in event['ruleParameters'] else None
        ds_hostname = event['ruleParameters']['dsHostname'] if 'dsHostname' in event['ruleParameters'] else None
        ds_port = event['ruleParameters']['dsPort'] if 'dsPort' in event['ruleParameters'] else 443
        ds_ignore_ssl_validation = distutils.util.strtobool(event['ruleParameters']['dsIgnoreSslValidation']) if 'dsIgnoreSslValidation' in event['ruleParameters'] else False
        mgr = None
        try:
            mgr = Manager(username=ds_username, password=ds_password, tenant=ds_tenant, hostname=ds_hostname, port=ds_port, ignore_ssl_validation=ds_ignore_ssl_validation)
            mgr.sign_in()
            print("Successfully authenticated to Deep Security")
        except Exception as err:
            print("Could not authenticate to Deep Security. Threw exception: {}".format(err))

        if mgr:
            control_names = {
                'anti_malware': 'Anti-Malware',
                'web_reputation': 'Web Reputation',
                'firewall': 'Firewall',
                'intrusion_prevention': 'Intrusion Prevention',
                'integrity_monitoring': 'Integrity Monitoring',
                'log_inspection': 'Log Inspection',
                }
            control_key = event['ruleParameters']['dsControl'].lower()

            mgr.computers.get()
            print("Searching {} computers for event source [{}]".format(len(mgr.computers), instance_id.lower().strip()))
            for comp_id, details in mgr.computers.items():
                if details.cloud_object_instance_id and (details.cloud_object_instance_id.lower().strip() == instance_id.lower().strip()):
                    print("Found matching computer. Deep Security #{}".format(comp_id))
                    control_status = getattr(details, 'overall_{}_status'.format(control_key))
                    detailed_msg = "{} status: {}".format(control_names[control_key], control_status)
                    print("...requested control [{}] reports: {}".format(control_key, detailed_msg))
                    if control_key in [ 'anti_malware', 'integrity_monitoring' ]:
                        if "On, Real Time".lower() in control_status.lower() or " On, Security Update In Progress, Real Time".lower() in control_status.lower():
                            is_protected = True
                            print("...is protected")
                    elif control_key in [ 'intrusion_prevention' ]:
                        if "On, Prevent".lower() in control_status.lower():
                            is_protected = True
                            print("...is protected")
                    else:
                        if "On".lower() in control_status.lower():
                            is_protected = True
                            print("...is protected")

            mgr.sign_out() # gracefully clean up our Deep Security session

    # Report the results back to AWS Config
    if detailed_msg:
        result = { 'annotation': detailed_msg }
    else:
        result = {}

    client = boto3.client('config')
    if instance_id:
        compliance = "NON_COMPLIANT"
        if is_protected:
            compliance = 'COMPLIANT'

        try:
            print("Sending results back to AWS Config")
            print('resourceId: {} is {}'.format(event['invokingEvent']['configurationItem']['resourceId'], compliance))

            evaluation = {
                'ComplianceResourceType': event['invokingEvent']['configurationItem']['resourceType'],
                'ComplianceResourceId': event['invokingEvent']['configurationItem']['resourceId'],
                'ComplianceType': compliance,
                'OrderingTimestamp': datetime.datetime.now()
            }

            if detailed_msg:
                evaluation['Annotation'] = detailed_msg

            response = client.put_evaluations(
                Evaluations=[evaluation],
                ResultToken=event['resultToken']
            )

            result['result'] = 'success'
            result['response'] = response
        except Exception as err:
            print("Exception thrown: {}".format(err))
            result['result'] = 'failure'

    print(result)
    return result
