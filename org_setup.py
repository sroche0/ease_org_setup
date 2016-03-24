import os
import argparse
import time
import json
import getpass
from sys import stdout
from subprocess import Popen, PIPE
from apperian import apperian


class EaseSetup:
    def __init__(self, params):
        self.user = params['user']
        self.password = params['password']
        self.php_endpoint = params['php']
        self.python_endpoint = params['py']
        self.keystore = params['keystore']
        self.sdk_path = params['sdk_path']
        self.sign_local = params['local']
        self.credentials = params['credentials_psk']
        self.ease = apperian.Ease(self.user, self.password, php=self.php_endpoint, py=self.python_endpoint,
                                  verbose=params['verbose'])
        self.app_list = self.ease.app.list()
        self.app_data = {
            'strongswan': {
                'file_name': params['vpn_apk'],
                'psk': params['vpn_psk'],
                'meta_data': params['vpn_metadata'],
                'policies': [0, 1, 3, 4]
            },
            'catalog': {
                'file_name': params['catalog_apk'],
                'psk': params['catalog_psk'],
                'meta_data': params['catalog_metadata'],
                'policies': [1, 6, 3, 4]
            }
        } if not params.get('app_data') else params['app_data']

    def device_init(self):
        # Check if PSKs and file_names were passed. If not, try to find them
        EaseSetup.file_check(self)
        EaseSetup.psk_check(self)

        print 'Preparing to Sideload {} apps'.format(len(self.app_data))

        # Make sure device is connected and usb debugging is on before proceeding
        valid = False
        while not valid:
            question = 'Is your device connected with USB Debugging enabled?'
            choice = raw_input('{} (y/n) :'.format(question))
            if choice in ['y', 'n']:
                valid = True
                if choice == 'y':
                    pass
                if choice == 'n':
                    print 'Please connect device and ensure USB Debugging is enabled in the device settings'
                    raw_input('Press any key to continue')
            else:
                print 'Please only choose "y" or "n"'

        # Set path for ADB
        adb_cmd = 'adb' if not self.sdk_path else '{}platform-tools/adb'.format(self.sdk_path)

        for index, app in enumerate(self.app_data):
            print
            print '-' * 40
            print 'Working on app {} of {}'.format(index + 1, len(self.app_data))
            print '-' * 40

            # If the binary file is not defined in config and not found in CWD, try to download from EASE
            if not app['file_name']:
                if app['psk']:
                    print 'App binary file is missing, but match was found on EASE'
                    msg = 'Downloading'
                    stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
                    stdout.flush()
                    download = self.ease.app.download(app['psk'])
                    if download['status'] == 200:
                        print 'Success'
                        app['file_name'] = download['result'].replace('.apk', '')
                    else:
                        print 'Failed'
                        print 'Unable to find binary for sideload, skipping...'
                        continue
                else:
                    print 'No file was found locally for app and no match found on EASE'
                    print 'Please ensure apk files are in directory with script and/or explicitly define them in config'
                    continue

            # Sideload to the device
            msg = 'Sideloading'
            stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
            stdout.flush()

            p = Popen([adb_cmd, 'install', '{}.apk'.format(app['file_name'])], stderr=PIPE, stdout=PIPE)
            outcome = p.communicate()

            if any('success' in x.lower() for x in outcome):
                print 'Success'
            else:
                print 'Failed'
                print outcome
                continue

        # After Sideloading, launch StrongSwan VPN for user to config
        time.sleep(1)
        print '\nLaunching StrongSwan VPN on device for configuration'
        p = Popen([adb_cmd, 'shell', 'monkey', '-p', 'org.strongswan.android', '-c',
                   'android.intent.category.LAUNCHER', '1'],
                  stderr=PIPE, stdout=PIPE)
        p.communicate()

    def org_init(self):
        # Check if PSKs and file_names were passed. If not, try to find them either in CWD or on EASE
        EaseSetup.file_check(self)
        EaseSetup.psk_check(self)

        if any(not x['file_name'] for x in self.app_data):
            print self.app_data
            print 'One or more file names were not defined in config and not found in current directory. Please make ' \
                  'sure all app binary files are in the directory with this script, and/or explicitly defined in the ' \
                  'config.json'
            exit('Quitting')

        # Iterate through the app_data from config.json, Uploading the apk, wrapping and signing it.
        for index, app in enumerate(self.app_data):
            # Upload the APKs to EASE
            if not app['meta_data']:
                app['meta_data'] = EaseSetup.get_metadata(app['file_name'])

            app_name = app['meta_data']['name']
            print
            print '-' * 40
            print 'Starting {} - App {}/{}'.format(app_name, index + 1, len(self.app_data))
            print '-' * 40

            # If app['psk'] was not passed or found, upload as a new app
            msg = 'Uploading'
            stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
            stdout.flush()
            if not app['psk']:
                upload_resp = self.ease.app.upload('{}.apk'.format(app['file_name']), app['meta_data'])

            # If app['psk'] exists, upload as an update to existing app
            else:
                upload_resp = self.ease.app.update(app['mask_psk'], app['meta_data'], '{}.apk'.format(app['file_name']))
            if upload_resp['status'] != 200:
                print 'Failed'
                print upload_resp
                continue
            print 'Success'

            # Convert masked psk to unmasked because the wrapping calls won't take a masked psk :(
            umask_psk = self.ease.app.get_details(upload_resp['result'])
            app['psk'] = umask_psk['result']['psk']

            # Wrap the APK if needed
            if app['policies']:
                print '\nWrapping app'
                # print 'e.wrapper.wrap_app({}, {})'.format(app['psk'], app['policies'])
                wrap_resp = self.ease.wrapper.wrap_app(app['psk'], app['policies'])

                # Update app_data with wrapping status. 1 = success, 0 = failure. continue loop and move on to next app
                # if wrapping fails
                if wrap_resp['status'] == 200:
                    app['wrapped'] = 1
                else:
                    app['wrapped'] = 0
                    continue

            # We need to download then sign the app, then reupload it to EASE
            if self.sign_local:
                print 'Signing App locally'
                zipalign_cmd = 'zipalign'
                if self.sdk_path:
                    if not os.path.isfile('{}build-tools/zipalign'.format(self.sdk_path)):
                        build_tools = sorted(os.listdir('{}build-tools/'.format(self.sdk_path)))
                        if len(build_tools) == 0:
                            print 'Your build-tools directory seems to be empty. Unable to continue wihout zipalign'
                            exit('Quitting')
                        else:
                            zipalign_cmd = '{}build-tools/{}/zipalign'.format(self.sdk_path, build_tools[-1])
                    else:
                        zipalign_cmd = '{}build-tools/zipalign'
                print

                msg = 'Downloading'
                stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
                stdout.flush()
                download = self.ease.app.download(app['psk'], '{}_wrapped.apk'.format(app['file_name']))
                if download['status'] != 200:
                    print 'Failed'
                    print download['result']
                    continue
                else:
                    print 'Success'

                # Sign the app
                msg = 'Signing'
                stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
                stdout.flush()
                p = Popen(['jarsigner', '-verbose', '-sigalg', 'SHA1withRSA', '-digestalg', 'SHA1', '-keystore',
                           self.keystore, '{}_wrapped.apk'.format(app['file_name']), 'alias_name'],
                          stdout=PIPE, stdin=PIPE)
                sign_resp = p.communicate()
                app['file_name'] += '_wrapped'

                # Align the apk
                msg = 'Aligning'
                stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
                stdout.flush()
                p = Popen([zipalign_cmd, '-v', '4', '{}_wrapped.apk'.format(app['file_name']),
                           '{}_aligned.apk'.format(app['file_name'])], stdin=PIPE, stdout=PIPE)
                align_resp = p.communicate()
                app['file_name'].replace('_wrapped', '_aligned')

                # Upload the newly signed apk to ease as an update
                msg = 'Uploading'
                stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
                stdout.flush()
                update = self.ease.app.update(app['mask_psk'], app['meta_data'], '{}.apk'.format(app['file_name']))
                if update['status'] == 200:
                    print 'Success'
                else:
                    print 'Failed'
                    print update['result']
                    continue

            else:
                print '\nSigning App'
                signing = self.ease.app.sign(app['psk'], self.credentials)
                if signing['status'] != 200:
                    print 'Failed'
                    print signing['result']
                    continue
                else:
                    self.ease.app.toggle(app['psk'], True)
                    msg = 'Downloading'
                    stdout.write('{}{}'.format(msg, '.' * (25 - len(msg))))
                    stdout.flush()

                    dl_response = self.ease.app.download(app['psk'], '{}_signed.apk'.format(app['file_name']))
                    if dl_response['status'] == 200:
                        print 'Success'
                        app['file_name'] += '_signed'
                    else:
                        print 'Failed'
                        print dl_response['result']
                        continue

    def org_update(self):
        pass

    def psk_check(self):
        self.app_list = self.ease.app.list()['result']
        masked_app_list = self.ease.publisher.get_list()['result']

        for app in self.app_data:
            # If both psk and masked psk are already set for this app, skip to next app
            if app['psk'] and app['mask_psk']:
                continue

            # Build lists of potential matches for the current app
            if app['type'] == 'catalog':
                matches = [(x['psk'], y['ID'], x['name']) for x in self.app_list for y in masked_app_list if
                           x['is_app_catalog'] is True and
                           x['operating_system'] in [102, 103, 104, 105] and
                           x['name'] == y['name']]
            elif app['type'] == 'vpn':
                matches = [(x['psk'], y['ID'], x['name']) for x in self.app_list for y in masked_app_list if
                            'strongswan' in x['name'].lower() and
                            x['name'] == y['name']]
            else:
                name = app['meta_data']['name']
                if name:
                    matches = [(x['psk'], y['ID'], x['name']) for x in self.app_list for y in masked_app_list if
                               name.replace(' ', '').lower() in x['name'].replace(' ', '').lower() and
                               x['name'] == y['name']]

            if len(matches) == 0:
                continue
            else:
                if len(matches) > 1:
                    print 'Multiple matches found in EASE. Please select one from the list below to update'
                    valid, choice = False, ''
                    print 'Possible Matches:'
                    for index, value in enumerate(matches):
                        print "    {}. {} - PSK: {}".format(index+1, value[2], value[0])

                    while not valid:
                        try:
                            choice = int(raw_input('\nPlease select the app you would like to update: '))
                            if 0 < choice <= len(matches):
                                valid = True
                                choice -= 1
                            else:
                                print 'Please select a valid option between 1 and {}'.format(len(matches))
                        except ValueError:
                            print "Please enter a number."

                    app['psk'] = matches[choice][0]
                    app['mask_psk'] = matches[choice][1]

                elif len(matches) == 1:
                    app['psk'] = matches[0][0]
                    app['mask_psk'] = matches[0][1]

    def file_check(self):
        if any(not x['file_name'] for x in self.app_data or not self.keystore):
            print 'Checking local directory for needed APK files...'
            file_list = os.listdir(os.getcwd())

            for app in self.app_data:
                if not app['file_name']:
                    matches = []
                    if app['type'] == 'catalog':
                        matches = [x for x in file_list if 'catalog' in x.lower() and '.apk' in x.lower()]
                    elif app['type'] == 'vpn':
                        matches = [x for x in file_list if 'strongswan' in x.lower() and '.apk' in x.lower()]
                    else:
                        name = app['meta_data']['name']
                        if name:
                            matches = [x for x in file_list if name.replace(' ', '').lower() in x.lower()]

                    if len(matches) == 1:
                        app['file_name'] = matches[0].replace('.apk', '')
                        print '    Using {}'.format(matches[0])
                    elif len(matches) > 1:
                        print 'More than one possible file match found in directory'
                        choice = EaseSetup.display_options(matches)
                        app['file_name'] = matches[choice].replace('.apk', '')
                        print '    Using {}'.format(matches[choice])

            if not self.keystore:
                matches = [x for x in file_list if '.keystore' in x.lower()]
                if len(matches) == 1:
                    self.keystore = matches[0]
                    print '    Found {}'.format(matches[0])
                elif len(matches) > 1:
                    print 'More than one possible file match found in directory'
                    choice = EaseSetup.display_options(matches)
                    self.keystore = matches[choice]
                    print '    Using {}'.format(matches[choice])

    def wrap_apps(self):
        pass

    @staticmethod
    def get_metadata(app):
        count = 0
        data = {}
        while True and count < 3:
            print "\nNo metadata passed for {}. Enter it below".format(app)

            data["author"] = raw_input('  Author: ')
            data["name"] = raw_input('  App Name: ')
            data["shortdescription"] = raw_input('  Short Description: ')
            data["longdescription"] = raw_input('  Long Description: ')
            data["version"] = raw_input('  Version: ')
            data["versionNotes"] = raw_input('  Version Notes: ')

            choice = raw_input('\nUpload {} with the above metadata? (y/n)'.format(app))
            if choice == 'y':
                return data
            elif choice == 'n':
                pass

    @staticmethod
    def display_options(input_list):
        valid, choice = False, ''
        for index, value in enumerate(input_list):
            print "    %s. %s" % (index+1, value)

        while not valid:
            try:
                choice = int(raw_input('\nPlease select from the list above: '))
                if 0 < choice <= len(input_list):
                    valid = True
                    choice -= 1
                else:
                    print 'Please select a valid option between 1 and {}'.format(len(input_list))
            except ValueError:
                print "Please enter a number."

        return choice


def get_args():
    p = argparse.ArgumentParser()
    p.add_argument('--user', '-u', metavar='username', default=None)
    p.add_argument('--password', '-pw', metavar='password', default=None)
    p.add_argument('--php', metavar='php endpoint. EX easesvc.apperian.com', default=None)
    p.add_argument('--py', metavar='python endpoint. EX ws.apperian.com', default=None)
    p.add_argument('--keystore', metavar='name of android keystore file', default=None)
    p.add_argument('--local', '--l', action='store_true', default=False)
    p.add_argument('--catalog_apk', '--c', metavar='catalog apk filename', default=None)
    p.add_argument('--catalog_psk', metavar='catalog psk from EASE', default=None)
    p.add_argument('--credentials_psk', metavar='credentials psk from EASE', default=None)
    p.add_argument('--catalog_metadata', metavar='metadata for upload to EASE', default=None)
    p.add_argument('--vpn_apk', '--v', metavar='strongswan apk filename', default=None)
    p.add_argument('--vpn_psk', metavar='strongswan psk from EASE', default=None)
    p.add_argument('--vpn_metadata', metavar='metadata for upload to EASE', default=None)
    p.add_argument('--sdk_path', '--sdk', metavar='android sdk path', default=None)
    p.add_argument('--verbose', default=False, action='store_true')

    # --action will be an int for what to do. By default just provision a device
    # 1 - provision a device
    # 2 - update catalog and vpn apks in an org
    # 3 - Both
    p.add_argument('--action', metavar='what actions to take', default='2')

    return vars(p.parse_args())


if __name__ == '__main__':
    print
    parameters = {}

    # Load config.json if it exists
    if os.path.isfile('config.json'):
        with open('config.json', 'rb') as f:
            parameters = json.load(f)

    passed_args = get_args()

    # Add missing keys and overwrite loaded configs with any passed params
    for key in passed_args.keys():
        if key not in parameters.keys():
            parameters[key] = passed_args[key]
        if passed_args[key]:
            parameters[key] = passed_args[key]

    if not parameters['user']:
        parameters['user'] = raw_input('Username: ')
    if not parameters['password']:
        parameters['password'] = getpass.getpass()

    setup = EaseSetup(parameters)
    if parameters['action'] in ('2', '3'):
        print 'Performing action {} - Upload new APKs to EASE'.format(parameters['action'])
        setup.org_init()
    if parameters['action'] in ('1', '3'):
        print 'Performing action {} - Provision a new device'.format(parameters['action'])
        setup.device_init()

    print 'Done'
