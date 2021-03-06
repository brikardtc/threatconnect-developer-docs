# -*- coding: utf-8 -*-
"""TcEx Framework Inputs module"""
import json
import os
import sys
from argparse import Namespace
from .argument_parser import TcArgumentParser


class Inputs(object):
    """Module for handling inputs passed to App from CLI, Config, SecureParams, and AOT

    Args:
        tcex (tcex.TcEx): Instance of TcEx class.
        config (dict): A dictionary containing the configuration data for tcex and App.
        config_file (str, optional): An filename containing JSON configuration data. Defaults to
            None.
    """

    def __init__(self, tcex, config, config_file=None):
        """Initialize Class Properties.

        Input Options:

        1. All inputs from CLI args
        2. Inputs from CLI arg and from AOT params
        3. Inputs from CLI arg and from secure params
        4. All inputs from Config params
        5. Inputs from Config params and from AOT params
        """
        self.tcex = tcex

        # properties
        self._parsed = False
        self._parsed_resolved = False

        # parser
        self.parser = TcArgumentParser()

        # handle config and config_file
        config_file_data = self.config_file(config_file)
        config.update(config_file_data)  # config_file params update config

        # create empty namespaces
        self._default_args = Namespace()
        self._default_args_resolved = Namespace()

        # update default_args namespace with config data using dict interface - for option #4 and #5
        self._default_args.__dict__.update(config)

        # parse CLI args - for option #1, #2, and #3 (additional parsing done in args() method)
        self._default_args, self._unknown_args = self.parser.parse_known_args(
            namespace=self._default_args
        )

        # update tcex default_args property for all dependent modules
        self.tcex._default_args = self._default_args

        # when running locally retrieve any args from the results.tc file
        self._results_tc_args()

        # register token as soon as possible (required for the any API call including secure params)
        self.register_token()

        # load aot params from redis (blocking) - for options #2 and #5
        self._load_aot_params()

        # load secure params from API - for options #3
        self._load_secure_params()

        # add default args namespace to parser for add_argument() method
        # used to covert any required args in Apps to default values from namespace
        self.parser.namespace = self._default_args

        # update logging now that all required tcex logging parameters are loaded
        self.update_logging()

    def _get_secure_params(self):
        """Load secure params from the API.

        # API Response:

        .. code-block:: javascript
            :linenos:
            :lineno-start: 1

            {
                "inputs":
                    {
                        "tc_playbook_db_type": "Redis",
                        "fail_on_error": true,
                        "api_default_org": "TCI"
                    }
            }

        Returns:
            dict: Parameters ("inputs") from the TC API.
        """
        self.tcex.log.info('Loading secure params.')

        # Retrieve secure params from API
        r = self.tcex.session.get('/internal/job/execution/parameters')

        # check for bad status code and response that is not JSON
        if not r.ok:
            err = r.text or r.reason
            raise RuntimeError('Error retrieving secure params from API ({}).'.format(err))

        secure_params = {}
        try:
            secure_params = r.json()['inputs']
        except (AttributeError, KeyError, TypeError, ValueError):  # pragma: no cover
            err = r.text or r.reason
            raise RuntimeError('Error retrieving secure params from API ({}).'.format(err))

        return secure_params

    def _load_aot_params(self):
        """Block and retrieve params from Redis."""
        if self._default_args.tc_aot_enabled:
            # update default_args with AOT params
            params = self.tcex.playbook.aot_blpop()
            updated_params = self.update_params(params)
            self.config(updated_params)

    def _load_secure_params(self):
        """Parse args and return default args."""
        if self._default_args.tc_secure_params:
            # update default_args with secure params from API
            params = self._get_secure_params()
            updated_params = self.update_params(params)
            self.config(updated_params)

    def _results_tc_args(self):  # pragma: no cover
        """Read data from results_tc file from previous run of app.

        This method is only required when not running from within the TcEX platform and is only
        intended for testing apps locally.
        """
        results = []
        if os.access(self.default_args.tc_out_path, os.W_OK):
            result_file = '{}/results.tc'.format(self.default_args.tc_out_path)
        else:
            result_file = 'results.tc'
        if os.path.isfile(result_file):
            with open(result_file, 'r') as rh:
                results = rh.read().strip().split('\n')
            os.remove(result_file)
        for line in results:
            if not line or ' = ' not in line:
                continue
            key, value = line.split(' = ')
            if value == 'true':
                value = True
            elif value == 'false':
                value = False
            elif not value:
                value = None
            setattr(self._default_args, key, value)

    def args(self, parse=False):
        """Parse args if they have not already been parsed and return the Namespace for args.

        .. Note:: Accessing args should only be done directly in the App.

        Returns:
            (namespace): ArgParser parsed arguments.
        """
        if not self._parsed or parse:
            # initialize default args
            args, self._unknown_args = self.parser.parse_known_args(namespace=self._default_args)
            self.config(args.__dict__, False)

            # special case for service Apps
            if self._default_args.tc_svc_client_topic is not None:
                # get the service id as third part of the service
                # --tc_svc_client_topic svc-client-cc66d36344787779ccaa8dbb5e09a7ab
                setattr(
                    self._default_args,
                    'service_id',
                    self._default_args.tc_svc_client_topic.split('-')[2],
                )

            # set parsed bool to ensure args are only parsed once
            self._parsed = True

            # log unknown arguments
            self.unknown_args()

        return self._default_args

    def config(self, config_data, preserve=True):
        """Add configuration data to update default_args.

        Below are the default args that the TcEx frameworks supports. Any App specific args
        should be included in the provided data.

        .. code-block:: javascript

            {
              "api_access_id": "$env.API_ACCESS_ID",
              "api_default_org": "$env.API_DEFAULT_ORG",
              "api_secret_key": "$envs.API_SECRET_KEY",
              "tc_api_path": "$env.TC_API_PATH",
              "tc_log_level": "debug",
              "tc_log_path": "log",
              "tc_owner": "MyOwner",
              "tc_proxy_host": "$env.TC_PROXY_HOST",
              "tc_proxy_password": "$envs.TC_PROXY_PASSWORD",
              "tc_proxy_port": "$env.TC_PROXY_PORT",
              "tc_proxy_tc": false,
              "tc_proxy_username": "$env.TC_PROXY_USERNAME"
            }

        Args:
            config (dict): A dictionary of configuration values.
            preserve (bool): Don't overwrite arg values define in sys.argv
        """
        if isinstance(config_data, dict):
            if preserve:
                # on env server core doesn't send all required values on cli. inputs that
                # come in via secureParams need to be updated, but not all of them (e.g. log_path).
                # this code will only update new inputs that are not provided via sys argv.
                for key in list(config_data):
                    if '--{}'.format(key) in sys.argv:
                        del config_data[key]

            # update the arg Namespace via dict
            self._default_args.__dict__.update(config_data)

            # register token as soon as possible
            self.register_token()

    def config_file(self, filename):
        """Load configuration data from provided file and update default_args.

        Args:
            config (str): The configuration file name.
        """
        if filename is not None:
            if os.path.isfile(filename):
                with open(filename, 'r') as fh:
                    return json.load(fh)
            else:
                self.tcex.log.error('Could not load configuration file "{}".'.format(filename))
        return {}

    @property
    def default_args(self):
        """Parse args and return default args."""
        return self._default_args

    @property
    def params(self):
        """Return input params."""
        # return self._default_args.__dict__
        self.args()  # ensure all args are parsed
        return self._default_args

    def register_token(self):
        """Register token if provided in args (non-service Apps)"""
        # TODO: swap MainThread with threading.current_thread().name ?
        if self._default_args.tc_token is not None:
            self.tcex.token.register_token(
                'MainThread', self._default_args.tc_token, self._default_args.tc_token_expires
            )

    def resolved_args(self, parse=False):
        """Return namespace of args that have all playbook variables automatically resolved.

        .. Note:: Accessing resolved_args should only be done directly in the App.

        Returns:
            (namespace): ArgParser parsed arguments with Playbook variables automatically resolved.
        """
        if not self._parsed_resolved or parse:  # only resolve once
            self.args()

            # create new args Namespace for resolved args
            self._default_args_resolved = Namespace()

            # iterate over args and resolve any playbook variables
            for arg in vars(self._default_args):
                arg_val = getattr(self._default_args, arg)
                if arg not in self.tc_reserved_args:
                    if isinstance(arg_val, (str)):
                        arg_val = self.tcex.playbook.read(arg_val)
                setattr(self._default_args_resolved, arg, arg_val)

            # set parsed bool to ensure args are only parsed once
            self._parsed_resolved = True
        return self._default_args_resolved

    @property
    def resolved_params(self):
        """Return input params."""
        # return self._default_args_resolved.__dict__
        return self._default_args_resolved

    @property
    def tc_bool_args(self):
        """Return a list of default ThreatConnect Args that are booleans."""
        return [
            'apply_proxy_external',
            'apply_proxy_ext',
            'apply_proxy_tc',
            'batch_halt_on_error',
            'tc_aot_enabled',
            'tc_log_to_api',
            'tc_proxy_external',
            'tc_proxy_tc',
            'tc_secure_params',
            'tc_verify',
        ]

    @property
    def tc_reserved_args(self):
        """Return a list of *all* ThreatConnect reserved arg values."""
        return [
            'tc_token',
            'tc_token_expires',
            'api_access_id',
            'api_secret_key',
            'batch_action',
            'batch_chunk',
            'batch_halt_on_error',
            'batch_poll_interval',
            'batch_interval_max',
            'batch_write_type',
            'tc_playbook_db_type',
            'tc_playbook_db_context',
            'tc_playbook_db_path',
            'tc_playbook_db_port',
            'tc_playbook_out_variables',
            'api_default_org',
            'tc_api_path',
            'tc_in_path',
            'tc_log_file',
            'tc_log_path',
            'tc_out_path',
            'tc_secure_params',
            'tc_temp_path',
            'tc_user_id',
            'tc_proxy_host',
            'tc_proxy_port',
            'tc_proxy_username',
            'tc_proxy_password',
            'tc_proxy_external',
            'tc_proxy_tc',
            'tc_log_to_api',
            'tc_log_level',
            'logging',
        ]

    def unknown_args(self):
        """Log argparser unknown arguments.

        Args:
            args (list): List of unknown arguments
        """
        for u in self._unknown_args:
            self.tcex.log.warning(u'Unsupported arg found ({}).'.format(u))

    def update_logging(self):
        """Update the TcEx logger with appropriate handlers."""
        if self._default_args.tc_log_level is None:
            # some Apps use logging while other us tc_log_level. ensure tc_log_level is always
            # available.
            self._default_args.tc_log_level = self._default_args.logging

        self.tcex.logger.log_info(self._default_args)

        # add api handler
        if self._default_args.tc_token is not None and self._default_args.tc_log_to_api:
            self.tcex.logger.add_api_handler(level=self.tcex.default_args.tc_log_level)

        # add rotating log handler
        self.tcex.logger.add_rotating_file_handler(
            name='rfh',
            filename=self._default_args.tc_log_file,
            path=self._default_args.tc_log_path,
            backup_count=self._default_args.tc_log_backup_count,
            max_bytes=self._default_args.tc_log_max_bytes,
            level=self.tcex.default_args.tc_log_level,
        )

        # replay cached log events
        self.tcex.logger.replay_cached_events(handler_name='cache')

    def update_params(self, params):
        """Update params provided by AOT and Secure Params to be of the proper value and type.

        Args:
            params (dict): A dictionary containing params to update default_args
        """
        updated_params = {}
        for arg, value in params.items():
            # ThreatConnect secure/AOT params could be updated in the future to proper JSON format.
            # MultiChoice data should be represented as JSON array and Boolean values should be a
            # JSON boolean and not a string.
            param_data = self.tcex.ij.params_dict.get(arg) or {}
            param_type = param_data.get('type', '').lower()
            param_allow_multiple = self.tcex.utils.to_bool(param_data.get('allowMultiple', False))

            if param_type == 'multichoice' or param_allow_multiple:
                # update delimited value to an array for params that have type of MultiChoice.
                if not isinstance(value, dict):
                    value = value.split(self.tcex.ij.list_delimiter)
            elif param_type == 'boolean':
                # convert boolean input that are passed in as a string ("true" -> True)
                value = self.tcex.utils.to_bool(value)
            elif arg in self.tc_bool_args:
                # convert default boolean args that are passed in as a string ("true" -> True)
                value = self.tcex.utils.to_bool(value)

            # add args and updated value to dict
            updated_params[arg] = value

        # update args
        return updated_params
