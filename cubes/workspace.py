# -*- coding=utf -*-
import sys
from .providers import read_model_metadata, create_model_provider
from .auth import create_authorizer, NoopAuthorizer
from .model import Model
from .common import get_logger
from .errors import *
from .stores import open_store, create_browser
from .calendar import Calendar
import os.path
import ConfigParser

__all__ = [
    "Workspace",

    # Depreciated
    "get_backend",
    "create_workspace",
    "create_workspace_from_config",
    "create_slicer_workspace",
    "create_slicer_context",
    "config_items_to_dict",
]

def config_items_to_dict(items):
    return dict([ (k, interpret_config_value(v)) for (k, v) in items ])

def interpret_config_value(value):
    if value is None:
        return value
    if isinstance(value, basestring):
        if value.lower() in ('yes', 'true', 'on'):
            return True
        elif value.lower() in ('no', 'false', 'off'):
            return False
    return value

def _get_name(obj, object_type="Object"):
    if isinstance(obj, basestring):
        name = obj
    else:
        try:
            name = obj["name"]
        except KeyError:
            raise ModelError("%s has no name" % object_type)

    return name


def create_slicer_workspace(server_url):
    cp = ConfigParser.SafeConfigParser()
    cp.add_section("datastore")
    cp.set("datastore", "type", "slicer")
    cp.set("datastore", "url", server_url)
    w = Workspace(cp)
    w.add_model({ 'name': 'slicer', 'store': 'slicer', 'provider': 'slicer' })
    return w

class Workspace(object):
    def __init__(self, config=None, stores=None):
        """Creates a workspace. `config` should be a `ConfigParser` or a
        path to a config file. `stores` should be a dictionary of store
        configurations, a `ConfigParser` or a path to a ``stores.ini`` file.
        """
        if isinstance(config, basestring):
            cp = ConfigParser.SafeConfigParser()
            try:
                cp.read(config)
            except Exception as e:
                raise ConfigurationError("Unable to load config %s. "
                                "Reason: %s" % (config, str(e)))

            config = cp

        elif not config:
            # Read ./slicer.ini
            config = ConfigParser.ConfigParser()

        self.store_infos = {}
        self.stores = {}

        self.logger = get_logger()

        if config.has_option("workspace", "log"):
            formatter = logging.Formatter(fmt='%(asctime)s %(levelname)s %(message)s')
            handler = logging.FileHandler(config.get("workspace", "log"))
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        if config.has_option("workspace", "log_level"):
            self.logger.setLevel(config.get("workspace", "log_level").upper())

        self.locales = []
        self.translations = []

        #
        # Model objects
        self._models = []

        # Object ownership – model providers will be asked for the objects
        self.cube_models = {}
        self.dimension_models = {}

        # Cache of created global objects
        self._cubes = {}
        self._dimensions = {}
        # Note: providers are responsible for their own caching

        # Register stores from external stores.ini file or a dictionary
        if isinstance(stores, basestring):
            store_config = ConfigParser.SafeConfigParser()
            try:
                store_config.read(stores)
            except Exception as e:
                raise ConfigurationError("Unable to read stores from %s. "
                                "Reason: %s" % (stores, str(e) ))

            for store in store_config.sections():
                self._register_store_dict(store,
                                          dict(store_config.items(store)))

        elif isinstance(stores, dict):
            for name, store in stores.items():
                self._register_store_dict(name, store)

        elif stores is not None:
            raise ConfigurationError("Unknown stores description object: %s" %
                                                    (type(stores)))

        # Calendar
        # ========

        if config.has_option("workspace", "timezone"):
            timezone = config.get("workspace", "timezone")
        else:
            timezone = None

        if config.has_option("workspace", "first_weekday"):
            first_weekday = config.get("workspace", "first_weekday")
        else:
            first_weekday = 0

        self.logger.debug("Workspace calendar timezone: %s first week day: %s"
                          % (timezone, first_weekday))
        self.calendar = Calendar(timezone=timezone,
                                 first_weekday=first_weekday)

        # Register stores
        #
        # * Default store is [datastore] in main config file
        # * Stores are also loaded from main config file from sections with
        #   name [store_*] (not documented feature)

        default = None
        if config.has_section("datastore"):
            default = dict(config.items("datastore"))
        elif config.has_section("workspace"):
            self.logger.warn("No [datastore] configuration found, using old "
                             "backend & [workspace]. Update you config file.")
            default = {}
            default = dict(config.items("workspace"))
            default["type"] = config.get("server", "backend") if config.has_option("server", "backend") else None

            if not default.get("type"):
                self.logger.warn("No store type specified, assuming 'sql'")
                default["type"] = "sql"

        if default:
            self._register_store_dict("default",default)

        # Register [store_*] from main config (not documented)
        for section in config.sections():
            if section.startswith("datastore_"):
                name = section[10:]
                self._register_store_dict(name, dict(config.items(section)))
            elif section.startswith("store_"):
                name = section[6:]
                self._register_store_dict(name, dict(config.items(section)))

        if config.has_section("browser"):
            self.browser_options = dict(config.items("browser"))
        else:
            self.browser_options = {}

        if config.has_section("main"):
            self.options = dict(config.items("main"))
        else:
            self.options = {}

        # Authorizer
        if config.has_option("workspace", "authorization"):
            auth_type = config.get("workspace", "authorization")
            options = dict(config.items("authorization"))
            self.authorizer = create_authorizer(auth_type, **options)
        else:
            self.authorizer = NoopAuthorizer()

        # Load models

        if config.has_section("model"):
            self.logger.warn("Section [model] is depreciated. Use 'model' in "
                             "[workspace] for single default model or use "
                             "section [models] to list multiple models.")
            if config.has_option("model", "path"):
                source = config.get("model", "path")
                self.logger.debug("Loading model from %s" % source)
                self.add_model(source)

        if config.has_option("workspace", "models_path"):
            models_path = config.get("workspace", "models_path")
        else:
            models_path = None

        models = []
        if config.has_option("workspace", "model"):
            models.append(config.get("workspace", "model"))
        if config.has_section("models"):
            models += [path for name, path in config.items("models")]

        self._load_models(models_path, models)

    def _load_models(self, root, paths):
        """Load `models` with root path `models_path`."""

        if root:
            self.logger.debug("Models root: %s" % root)
        else:
            self.logger.debug("Models root set to current directory")

        for path in paths:
            self.logger.debug("Loading model %s" % path)
            if root and not os.path.isabs(path):
                path = os.path.join(root, path)
            self.add_model(path)

    def _register_store_dict(self, name, info):
        info = dict(info)
        try:
            type_ = info.pop("type")
        except KeyError:
            try:
                type_ = info.pop("backend")
            except KeyError:
                raise ConfigurationError("Datastore '%s' has no type specified" % name)
            else:
                self.logger.warn("'backend' is depreciated, use 'type' for "
                                 "datastore (in %s)." % str(name))

        self.register_store(name, type_, **info)

    def register_default_store(self, type_, **config):
        """Convenience function for registering the default store. For more
        information see `register_store()`"""
        self.register_store("default", type_, **config)

    def register_store(self, name, type_, **config):
        """Adds a store configuration."""

        if name in self.store_infos:
            raise ConfigurationError("Store %s already registered" % name)

        self.store_infos[name] = (type_, config)

    def _store_for_model(self, metadata):
        """Returns a store for model specified in `metadata`. """
        store_name = metadata.get("datastore")
        if not store_name and "info" in metadata:
            store_name = metadata["info"].get("datastore")

        store_name = store_name or "default"

        return store_name

    def add_model(self, model, name=None, store=None, translations=None):
        """Registers the `model` in the workspace. `model` can be a metadata
        dictionary, filename, path to a model bundle directory or a URL.

        If `name` is specified, then it is used instead of name in the
        model. `store` is an optional name of data store associated with the
        model.

        Model is added to the list of workspace models. Model provider is
        determined and associated with the model. Provider is then asked to
        list public cubes and public dimensions which are registered in the
        workspace.

        No actual cubes or dimensions are created at the time of calling this
        method. The creation is deffered until :meth:`cubes.Workspace.cube` or
        :meth:`cubes.Workspace.dimension` is called.

        """

        # Model -> Store -> Provider

        if isinstance(model, basestring):
            metadata = read_model_metadata(model)
        elif isinstance(model, dict):
            metadata = model
        else:
            raise ConfigurationError("Unknown model source reference '%s'" % model)

        # Master model?
        if metadata.get("__is_master"):
            # Other places using this format:
            #     Workspace.model()
            #     slicer tool merge_model

            parts = metadata["parts"]
            self.logger.debug("loading master model parts (%s)" % len(parts))
            for part in parts:
                self.add_model(part)
            return

        model_name = name or metadata.get("name")

        # Get a provider as specified in the model by "provider" or get the
        # "default provider"
        provider_name = metadata.get("provider", "default")
        provider = create_model_provider(provider_name, metadata)

        if provider.requires_store():
            store_name = store or self._store_for_model(metadata)
            store = self.get_store(store_name)
            provider.set_store(store, store_name)

        model_object = Model(metadata=metadata,
                             provider=provider,
                             translations=translations)
        model_object.name = metadata.get("name")

        self._models.append(model_object)

        # Get list of static or known cubes
        # "cubes" might be a list or a dictionary, depending on the provider
        for cube in provider.list_cubes():
            name = _get_name(cube, "Cube")
            # self.logger.debug("registering public cube '%s'" % name)
            self._register_public_cube(name, model_object)

        # Register public dimensions
        for dim in provider.public_dimensions():
            if dim:
                self._register_public_dimension(dim, model_object)

    def _register_public_dimension(self, name, model):
        if name in self.dimension_models:
            model_name = model.name or "(unknown)"
            previous_name = self.dimension_models[name].name or "(unknown)"

            raise ModelError("Duplicate public dimension '%s' in model %s, "
                             "previous model: %s" %
                                        (name, model_name, previous_name))

        self.dimension_models[name] = model

    def _register_public_cube(self, name, model):
        if name in self.cube_models:
            model_name = model.name or "(unknown)"
            previous_name = self.cube_models[name].name or "(unknown)"

            raise ModelError("Duplicate public cube '%s' in model %s, "
                             "previous model: %s" %
                                        (name, model_name, previous_name))

        self.cube_models[name] = model

    def model(self):
        """Return a master model. Master model can be used to reconstruct the
        workspace.

        .. note::

            Master model should not be edited by hand for now.

        .. note::

            Avoid using this method.
        """

        master_model = {}

        master_model["__comment"] = "This is a master model. Do not edit."
        master_model["__is_master"] = True

        models = [model.metadata for model in self._models]
        master_model["parts"] = models

        return master_model

    def list_cubes(self):
        """Get a list of metadata for cubes in the workspace. Result is a list
        of dictionaries with keys: `name`, `label`, `category`, `info`.

        The list is fetched from the model providers on the call of this
        method.
        """
        all_cubes = []
        for model in self._models:
            all_cubes += model.provider.list_cubes()

        return all_cubes

    def cube(self, name):
        """Returns a cube with `name`"""

        if not isinstance(name, basestring):
            raise TypeError("Name is not a string, is %s" % type(name))

        if name in self._cubes:
            return self._cubes[name]

        # Requirements:
        #    all dimensions in the cube should exist in the model
        #    if not they will be created

        try:
            model = self.cube_models[name]
        except KeyError:
            model = self._model_for_cube(name)

        provider = model.provider
        cube = provider.cube(name)

        if not cube:
            raise NoSuchCubeError(name, "No cube '%s' returned from "
                                     "provider." % name)

        self.link_cube(cube, model, provider)

        self._cubes[name] = cube

        return cube

    def _model_for_cube(self, name):
        """Discovers the first model that can provide cube with `name`"""

        # Note: this will go through all model providers and gets a list of
        # provider's cubes. Might be a bit slow, if the providers get the
        # models remotely.

        model = None
        for model in self._models:
            cubes = model.provider.list_cubes()
            names = [cube["name"] for cube in cubes]
            if name in names:
                break

        if not model:
            raise ModelError("No model for cube '%s'" % name)

        return model

    def link_cube(self, cube, model, provider):
        """Links dimensions to the cube in the context of `model` with help of
        `provider`."""

        # Assumption: empty cube

        # Algorithm:
        #     1. Give a chance to get the dimension from cube's provider
        #     2. If provider does not have the dimension, then use a public
        #        dimension
        #
        # Note: Provider should not raise `TemplateRequired` for public
        # dimensions
        #

        for dim_name in cube.linked_dimensions:
            try:
                dim = provider.dimension(dim_name)
            except NoSuchDimensionError:
                dim = self.dimension(dim_name)
            except TemplateRequired as e:
                # FIXME: handle this special case
                raise ModelError("Template required in non-public dimension "
                                 "'%s'" % dim_name)

            cube.add_dimension(dim)

    def dimension(self, name):
        """Returns a dimension with `name`. Raises `NoSuchDimensionError` when
        no model published the dimension. Raises `RequiresTemplate` error when
        model provider requires a template to be able to provide the
        dimension, but such template is not a public dimension."""

        if name in self._dimensions:
            return self._dimensions[name]

        # Assumption: all dimensions that are to be used as templates should
        # be public dimensions. If it is a private dimension, then the
        # provider should handle the case by itself.
        missing = set( (name, ) )
        while missing:
            dimension = None
            deferred = set()

            name = missing.pop()

            # Get a model that provides the public dimension. If no model
            # advertises the dimension as public, then we fail.
            try:
                model = self.dimension_models[name]
            except KeyError:
                raise NoSuchDimensionError("No public dimension '%s'" % name)

            try:
                dimension = model.provider.dimension(name, self._dimensions)
            except TemplateRequired as e:
                missing.add(name)
                if e.template in missing:
                    raise ModelError("Dimension templates cycle in '%s'" %
                                        e.template)
                missing.add(e.template)
                continue
            except NoSuchDimensionError:
                dimension = self._dimensions.get("name")
            else:
                # We store the newly created public dimension
                self._dimensions[name] = dimension


            if not dimension:
                raise NoSuchDimensionError("Missing dimension: %s" % name, name)

        return dimension

    def _browser_options(self, cube):
        """Returns browser configuration options for `cube`. The options are
        taken from the configuration file and then overriden by cube's
        `browser_options` attribute."""

        options = dict(self.browser_options)
        if cube.browser_options:
            options.update(cube.browser_options)

        return options

    def browser(self, cube, locale=None):
        """Returns a browser for `cube`."""

        # TODO: bring back the localization
        # model = self.localized_model(locale)

        if isinstance(cube, basestring):
            cube = self.cube(cube)

        # TODO: check if the cube is "our" cube

        store_name = cube.datastore or "default"
        store = self.get_store(store_name)
        store_type = self.store_infos[store_name][0]
        store_info = self.store_infos[store_name][1]

        options = self._browser_options(cube)

        # TODO: merge only keys that are relevant to the browser!
        options.update(store_info)

        # TODO: Construct options for the browser from cube's options dictionary and
        # workspece default configuration
        #

        browser_name = cube.browser
        if not browser_name and hasattr(store, "default_browser_name"):
            browser_name = store.default_browser_name
        if not browser_name:
            browser_name = store_type
        if not browser_name:
            raise ConfigurationError("No store specified for cube '%s'" % cube)

        browser = create_browser(browser_name, cube, store=store,
                                 locale=locale, **options)

        return browser

    def cube_features(self, cube):
        """Returns browser features for `cube`"""
        # TODO: this might be expensive, make it a bit cheaper
        # recycle the feature-providing browser or something. Maybe use class
        # method for that
        return self.browser(cube).features()

    def get_store(self, name="default"):
        """Opens a store `name`. If the store is already open, returns the
        existing store."""

        if name in self.stores:
            return self.stores[name]

        try:
            type_, options = self.store_infos[name]
        except KeyError:
            raise ConfigurationError("No info for store %s" % name)

        store = open_store(type_, **options)
        self.stores[name] = store
        return store

    def close(self):
        """Closes the workspace with all open stores and other associated
        resources."""

        for store in self.open_stores:
            store.close()


# TODO: Remove following depreciated functions

def get_backend(name):
    raise NotImplementedError("get_backend() is depreciated. "
                              "Use Workspace instead." )


def create_slicer_context(config):
    raise NotImplementedError("create_slicer_context() is depreciated. "
                              "Use Workspace instead." )


def create_workspace(backend_name, model, **options):
    """Depreciated. Use the following instead:

    .. code-block:: python

        ws = Workspace()
        ws.add_model(model)
        ws.register_store("default", backend_name, **options)
    """

    workspace = Workspace()
    workspace.add_model(model)
    workspace.register_store("default", backend_name, **options)
    workspace.logger.warn("create_workspace() is depreciated, "
                          "use Workspace(config) instead")
    return workspace


def create_workspace_from_config(config):
    """Depreciated. Use the following instead:

    .. code-block:: python

        ws = Workspace(config)
    """

    workspace = Workspace(config=config)
    workspace.logger.warn("create_workspace_from_config() is depreciated, "
                          "use Workspace(config) instead")
    return workspace

