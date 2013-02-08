#!/usr/bin/python
#
# Copyright (c) 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

import glob
import imp
import inspect
import os
import sys

from iniparse import SafeConfigParser
from iniparse.compat import NoSectionError, NoOptionError

from subscription_manager.base_plugin import SubManPlugin

import logging
log = logging.getLogger('rhsm-app.' + __name__)

from rhsm.config import initConfig
cfg = initConfig()

import gettext
_ = gettext.gettext

# The API_VERSION constant defines the current plugin API version. It is used
# to decided whether or not plugins can be loaded. It is compared against the
# 'requires_api_version' attribute of each plugin. The version number has the
# format: "major_version.minor_version".
#
# For a plugin to be loaded, the major version required by the plugin must match
# the major version in API_VERSION. Additionally, the minor version in
# API_VERSION must be greater than or equal the minor version required by the
# plugin.
#
# If a change is made that breaks backwards compatibility with regard to the plugin
# API, the major version number must be incremented and the minor version number
# reset to 0. If a change is made that doesn't break backwards compatibility,
# then the minor number must be incremented.
API_VERSION = "1.0"

SLOTS = [
    "pre_product_id_install",
    "post_product_id_install",
    "pre_register_consumer",
    "post_register_consumer",
    ]


class PluginException(Exception):
    def _add_message(self, repr):
        if hasattr(self, "msg") and self.msg:
            repr = "\n".join([repr, "Message: %s" % self.msg])
        return repr


class PluginImportException(PluginException):
    def __init__(self, module_file, module_name, msg=None):
        self.module_file = module_file
        self.module_name = module_name
        self.msg = msg

    def __str__(self):
        repr = "Plugin \"%s\" can't be imported from file %s" % \
            (self.module_name, self.module_file)
        return self._add_message(repr)


class PluginImportApiVersionException(PluginImportException):
    def __init__(self, module_file, module_name, module_ver, api_ver, msg=None):
        self.module_file = module_file
        self.module_name = module_name
        self.module_ver = module_ver
        self.api_ver = api_ver
        self.msg = msg

    def __str__(self):
        repr = "Plugin \"%s\" requires API version %s. Supported API is %s" % \
            (self.module_name, self.module_ver, self.api_ver)
        return self._add_message(repr)


class PluginConfigException(PluginException):
    def __init__(self, plugin_name, msg=None):
        self.plugin_name = plugin_name
        self.msg = msg

    def __str__(self):
        repr = "Cannot load configuration for plugin \"%s\"" % (self.plugin_name)
        return self._add_message(repr)


class BaseConduit(object):
    slots = []

    def __init__(self, clazz, conf):
        self._conf = conf

        self.log = logging.getLogger("rhsm-app." + clazz.__name__)

    def confString(self, section, option, default=None):
        try:
            self._conf.get(section, option)
        except (NoSectionError, NoOptionError):
            if default is None:
                return None
            return str(default)

    def confBool(self, section, option, default=None):
        try:
            self._conf.getboolean(section, option)
        except (NoSectionError, NoOptionError):
            if default is True:
                return True
            elif default is False:
                return False
            else:
                raise ValueError("Boolean value expected")

    def confInt(self, section, option, default=None):
        try:
            self._conf.getint(section, option)
        except (NoSectionError, NoOptionError):
            try:
                val = int(default)
            except (ValueError, TypeError):
                raise ValueError("Integer value expected")
            return val

    def confFloat(self, section, option, default=None):
        try:
            self._conf.getfloat(section, option)
        except (NoSectionError, NoOptionError):
            try:
                val = float(default)
            except (ValueError, TypeError):
                raise ValueError("Float value expected")
            return val


class ProductConduit(BaseConduit):
    slots = ['pre_product_id_install', 'post_product_id_install']

    def __init__(self, clazz, conf, product_list):
        super(ProductConduit, self).__init__(clazz, conf)
        self.product_list = product_list

    def getProductList(self):
        return self.product_list


class RegistrationConduit(BaseConduit):
    slots = ['pre_register_consumer', 'post_register_consumer']

    def __init__(self, clazz, conf, name, facts):
        super(RegistrationConduit, self).__init__(clazz, conf)
        self.name = name
        self.facts = facts

    def getName(self):
        return self.name

    def getFacts(self):
        return self.facts


class BasePluginManager(object):
    def __init__(self, search_path, plugin_conf_path):
        self.search_path = search_path
        self.plugin_conf_path = plugin_conf_path

        self._plugins = {}
        self._plugin_funcs = {}
        self.conduits = [BaseConduit, ProductConduit, RegistrationConduit]
        self._slot_to_conduit = {}

        for conduit_class in self.conduits:
            slots = conduit_class.slots
            for slot in slots:
                self._slot_to_conduit[slot] = conduit_class

        for slot in SLOTS:
            self._plugin_funcs[slot] = []

        self._import_plugins()

    def run(self, slot_name, **kwargs):
        log.debug("PluginManager.run called for %s with args: %s" % (slot_name, kwargs))
        for func in self._plugin_funcs[slot_name]:
            plugin_key = ".".join([func.im_class.__module__, func.im_class.__name__])
            log.debug("Running %s in %s" % (func.im_func.func_name, plugin_key))
            # resolve slot_name to conduit
            conduit = self._slot_to_conduit[slot_name]

            instance, conf = self._plugins[plugin_key]
            func(conduit(func.im_class, conf, **kwargs))

    def _import_plugins(self):
        """Load all the plugins in the search path."""
        if not os.path.isdir(self.search_path):
            log.error("Could not find %s for plugin import" % self.search_path)
            return

        mask = os.path.join(self.search_path, "*.py")
        for module_file in sorted(glob.glob(mask)):
            try:
                self._load_plugin(module_file)
            except PluginException, e:
                log.error(e)
        loaded_plugins = ", ".join(self._plugins)
        log.debug("Loaded plugins: %s" % loaded_plugins)

    def _load_plugin(self, module_file):
        """Load an individual plugin."""
        dir, module_name = os.path.split(module_file)
        module_name = module_name.split(".py")[0]

        try:
            fp, pathname, description = imp.find_module(module_name, [dir])
            try:
                module = imp.load_module(module_name, fp, pathname, description)
            finally:
                fp.close()
        except:
            raise PluginImportException(module_file, module_name)

        if not hasattr(module, "requires_api_version"):
            raise PluginImportException(module_file, module_name, "Plugin doesn't specify required API version")
        if not api_version_ok(API_VERSION, module.requires_api_version):
            raise PluginImportApiVersionException(module_file, module_name, module_ver=module.requires_api_version, api_ver=API_VERSION)

        def is_plugin(c):
            return inspect.isclass(c) and c.__module__ == module_name and issubclass(c, SubManPlugin)

        plugin_classes = inspect.getmembers(sys.modules[module_name], is_plugin)
        for name, clazz in sorted(plugin_classes):
            plugin_key = ".".join([module_name, name])
            conf = self._get_plugin_conf(plugin_key)

            try:
                enabled = conf.getboolean('main', 'enabled')
            except Exception, e:
                raise PluginConfigException(plugin_key, e)

            if not enabled:
                log.debug("Not loading \"%s\" plugin as it is disabled" % plugin_key)
                return

            instance = clazz()
            if plugin_key not in self._plugins:
                self._plugins[plugin_key] = (instance, conf)
            else:
                # This shouldn't ever happen
                raise PluginException("Two or more plugins with the name \"%s\" exist " \
                    "in the plugin search path" % name)

            for slot in SLOTS:
                func_name = slot + "_hook"
                if hasattr(instance, func_name):
                    self._plugin_funcs[slot].append(getattr(instance, func_name))

    def _get_plugin_conf(self, plugin_key):
        conf_file = os.path.join(self.plugin_conf_path, plugin_key + ".conf")
        if not os.access(conf_file, os.R_OK):
            raise PluginConfigException(plugin_key, "Unable to find configuration file")

        parser = SafeConfigParser()
        try:
            parser.read(conf_file)
        except Exception, e:
            raise PluginConfigException(plugin_key, e)
        return parser


#NOTE: need to be super paranoid here about existing of cfg variables
# BasePluginManager with our default config info
class PluginManager(BasePluginManager):
    def __init__(self, search_path=None, plugin_conf_path=None):
        init_search_path = search_path or cfg.get("rhsm", "pluginDir")
        init_plugin_conf_path = plugin_conf_path or cfg.get("rhsm", "pluginConfDir")
        super(PluginManager, self).__init__(search_path=init_search_path,
                                            plugin_conf_path=init_plugin_conf_path)


def parse_version(api_version):
    maj, min = api_version.split('.')
    return int(maj), int(min)


def api_version_ok(a, b):
    """
    Return true if API version "a" supports API version "b"
    """
    a = parse_version(a)
    b = parse_version(b)

    if a[0] != b[0]:
        return False

    if a[1] >= b[1]:
        return True

    return False
