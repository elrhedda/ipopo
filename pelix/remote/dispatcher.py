#!/usr/bin/env python
# -- Content-Encoding: UTF-8 --
"""
Pelix remote services: Common dispatcher

Calls services according to the given method name and parameters

:author: Thomas Calmant
:copyright: Copyright 2013, isandlaTech
:license: Apache License 2.0
:version: 0.2
:status: Beta

..

    Copyright 2013 isandlaTech

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""

# Module version
__version_info__ = (0, 2, 0)
__version__ = ".".join(str(x) for x in __version_info__)

# Documentation strings format
__docformat__ = "restructuredtext en"

# ------------------------------------------------------------------------------

# Remote Services constants
import pelix.remote
from pelix.remote import RemoteServiceError

# HTTP constants
import pelix.http

# iPOPO decorators
from pelix.ipopo.decorators import ComponentFactory, Requires, Provides, \
    BindField, Property, Validate, Invalidate, Instantiate, UnbindField
from pelix.utilities import to_str, Deprecated

# Standard library
import json
import logging
import threading

try:
    # Python 3
    import http.client as httplib
    from urllib.parse import urljoin

except ImportError:
    # Python 2
    import httplib
    from urlparse import urljoin

# ------------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------

@ComponentFactory('pelix-remote-dispatcher-factory')
@Provides(pelix.remote.SERVICE_DISPATCHER)
@Requires('_exporters', pelix.remote.SERVICE_EXPORT_PROVIDER, True, True)
@Requires('_importers', pelix.remote.SERVICE_IMPORT_PROVIDER, True, True)
@Requires('_listeners', pelix.remote.SERVICE_ENDPOINT_LISTENER, True, True,
          "(listen.exported=*)")
@Instantiate('pelix-remote-dispatcher')
class Dispatcher(object):
    """
    Common dispatcher for all exporters
    """
    def __init__(self):
        """
        Sets up the component
        """
        # Remote Service providers
        self._exporters = []
        self._importers = []

        # Injected listeners
        self._listeners = []

        # Framework UID
        self._fw_uid = None

        # Kind -> {Name -> Endpoint}
        self.__kind_endpoints = {}

        # UID -> Endpoint
        self.__endpoints = {}

        # UID -> Exporter
        self.__uid_exporter = {}

        # Service Reference -> set(UID)
        self.__service_uids = {}

        # Lock
        self.__lock = threading.Lock()

        # Validation flag
        self.__validated = False


    @Validate
    def validate(self, context):
        """
        Component validated
        """
        # Get the framework UID
        self._fw_uid = context.get_property(pelix.framework.FRAMEWORK_UID)
        self._context = context
        self.__validated = True

        # Prepare the export LDAP filter
        ldapfilter = '(|({0}=*)({1}=*))' \
                            .format(pelix.remote.PROP_EXPORTED_CONFIGS,
                                    pelix.remote.PROP_EXPORTED_INTERFACES)

        # Export existing services
        existing_ref = context.get_all_service_references(None, ldapfilter)
        if existing_ref is not None:
            for reference in existing_ref:
                self.__export_service(reference)

        # Register a service listener, to update the exported services state
        context.add_service_listener(self, ldapfilter)


    @Invalidate
    def invalidate(self, context):
        """
        Component invalidated: clean up storage
        """
        # Unregister the service listener
        context.remove_service_listener(self)

        self.__validated = False
        self._context = None
        self._fw_uid = None


    def _compute_endpoint_name(self, properties):
        """
        Computes the end point name according to service properties

        :param properties: Service properties
        :return: The computed end point name
        """
        name = properties.get(pelix.remote.PROP_ENDPOINT_NAME)
        if not name:
            name = "service_{0}".format(properties[pelix.constants.SERVICE_ID])

        return name


    def service_changed(self, event):
        """
        Called when a service event is triggered
        """
        kind = event.get_kind()
        svc_ref = event.get_service_reference()

        if kind == pelix.framework.ServiceEvent.REGISTERED:
            # Simply export the service
            self.__export_service(svc_ref)

        elif kind == pelix.framework.ServiceEvent.MODIFIED:
            # Matching registering or updated service
            if svc_ref not in self.__service_uids:
                # New match
                self.__export_service(svc_ref)

            else:
                # Properties modification:
                # Re-export if endpoint.name has changed
                self.__update_service(svc_ref, event.get_previous_properties())

        elif svc_ref in self.__service_uids and \
                (kind == pelix.framework.ServiceEvent.UNREGISTERING or \
                 kind == pelix.framework.ServiceEvent.MODIFIED_ENDMATCH):
            # Service is updated or unregistering
            self.__unexport_service(svc_ref)


    def __export_service(self, svc_ref):
        """
        Exports the given service using all available matching providers

        :param svc_ref: A service reference
        """
        # Service can be exported
        service_uids = self.__service_uids.setdefault(svc_ref, set())

        if not self._exporters:
            _logger.warning("No exporters yet.")
            return

        configs = svc_ref.get_property(pelix.remote.PROP_EXPORTED_CONFIGS)
        if not configs or configs == '*':
            # Export with all providers
            exporters = self._exporters[:]

        else:
            # Filter exporters
            exporters = [exporter for exporter in self._exporters[:]
                         if exporter.handles(configs)]

        if not exporters:
            _logger.warning("No exporter for %s", configs)
            return

        # Get common values
        name = self._compute_endpoint_name(svc_ref.get_properties())

        # Create endpoints
        endpoints = []
        for exporter in exporters:
            try:
                # Create the endpoint
                endpoint = exporter.export_service(svc_ref, name, self._fw_uid)
                if endpoint is None:
                    # Export refused
                    continue

                endpoints.append(endpoint)

                # Store it
                uid = endpoint.uid
                self.__endpoints[uid] = endpoint
                self.__uid_exporter[uid] = exporter
                service_uids.add(uid)

            except (NameError, pelix.constants.BundleException) as ex:
                _logger.error("Error exporting service: %s", ex)

        if not endpoints:
            _logger.warning("No endpoint created for %s", svc_ref)
            return

        # Call listeners (out of the lock)
        if self._listeners:
            for listener in self._listeners[:]:
                listener.endpoints_added(endpoints)


    def __update_service(self, svc_ref, old_properties):
        """
        Service updated, notify exporters
        """
        try:
            # Get the UIDs of its endpoints
            uids = self.__service_uids[svc_ref].copy()

        except KeyError:
            # No known UID
            return

        for uid in uids:
            try:
                # Get its exporter and bean
                exporter = self.__uid_exporter[uid]
                endpoint = self.__endpoints[uid]

            except KeyError:
                # No exporter
                _logger.warning("No exporter for endpoint %s", uid)

                # Remove the UID from the storage
                self.__service_uids[svc_ref].retain(uid)

            else:
                # TODO: check configuration change (can be an unexport)

                # Compute the previous name
                new_name = self._compute_endpoint_name(svc_ref.get_properties())

                try:
                    exporter.update_export(endpoint, new_name, old_properties)

                except NameError as ex:
                    _logger.error("Error updating service properties: %s", ex)
                    exporter.unexport_service(endpoint)
                    # Call listeners (out of the lock)
                    if self._listeners:
                        for listener in self._listeners:
                            listener.endpoint_removed(endpoint, old_properties)

                else:
                    # Call listeners (out of the lock)
                    if self._listeners:
                        for listener in self._listeners:
                            listener.endpoint_updated(endpoint, old_properties)


    def __unexport_service(self, svc_ref):
        """
        Deletes all endpoints for the given service

        :param svc_ref: A service reference
        """
        try:
            # Get the UIDs of its endpoints
            uids = self.__service_uids.pop(svc_ref)

        except KeyError:
            # No known UID
            return

        for uid in uids:
            try:
                # Remove from storage
                endpoint = self.__endpoints.pop(uid)
                exporter = self.__uid_exporter.pop(uid)

            except KeyError:
                # Oops
                _logger.warning("Trying to remove a lost endpoint (%s)", uid)

            else:
                # Delete endpoint
                exporter.unexport_service(endpoint)

                # Call listeners
                if self._listeners:
                    for listener in self._listeners[:]:
                        try:
                            listener.endpoint_removed(endpoint)

                        except Exception as ex:
                            _logger.error("Error notifying listener: %s", ex)



    @BindField('_listeners')
    def _bind_listener(self, field, listener, svc_ref):
        """
        Listener bound to the component
        """
        # Exported services listener
        if self.__endpoints:
            try:
                listener.endpoints_added(list(self.__endpoints.values()))

            except Exception as ex:
                _logger.exception("Error notifying newly bound listener: %s",
                                  ex)


    @BindField('_exporters')
    def _bind_exporter(self, field, exporter, svc_ref):
        """
        Exporter bound
        """
        # Do nothing if no yet validated
        if not self.__validated:
            return

        # Tell the exporter to export already known services
        for svc_ref in self.__service_uids:
            # Compute the endpoint name
            name = self._compute_endpoint_name(svc_ref)

            try:
                # Create the endpoint
                endpoint = exporter.export_service(svc_ref, name, self._fw_uid)

                # Store it
                uid = endpoint.uid
                self.__endpoints[uid] = endpoint
                self.__uid_exporter[uid] = exporter
                self.__service_uids.setdefault(svc_ref, set()).add(uid)

            except (NameError, pelix.constants.BundleException) as ex:
                _logger.error("Error exporting service: %s", ex)

            else:
                # Call listeners (out of the lock)
                if self._listeners:
                    for listener in self._listeners[:]:
                        listener.endpoints_added([endpoint])


    @UnbindField('_exporters')
    def _unbind_exporter(self, field, exporter, svc_ref):
        """
        Exporter gone
        """
        # TODO: delete all endpoints from this provider
        pass


    def get_endpoint(self, uid):
        """
        Retrieves an end point description, selected by its UID.
        Returns None if the UID is unknown.

        :param uid: UID of an end point
        :return: The end point description
        """
        return self.__endpoints.get(uid)


    def get_endpoints(self, kind=None, name=None):
        """
        Retrieves all end points matching the given kind and/or name

        :param kind: A kind of end point
        :param name: The name of the end point
        :return: A list of end point matching the parameters
        """
        with self.__lock:
            # Get all endpoints
            endpoints = list(self.__endpoints.values())

        # Filter by name
        if name:
            endpoints = [endpoint for endpoint in endpoints
                         if endpoint.name == name]

        # Filter by kind
        if kind:
            endpoints = [endpoint for endpoint in endpoints
                         if kind in endpoint.configurations]

        return endpoints


    @Deprecated("API will change")
    def get_service(self, kind, name):
        """
        Retrieves the instance of the service at the given end point for the
        given kind.

        :param kind: A kind of end point
        :param name: The name of the end point
        :return: The service corresponding to the given end point, or None
        """
        _logger.critical("Calling a deprecated method")
        try:
            return self.__kind_endpoints[kind][name].instance

        except KeyError:
            return None


    @Deprecated("Method will disappear")
    def dispatch(self, kind, name, method, params):
        """
        Calls the service for the given kind with the name

        :param kind: A kind of end point
        :param name: The name of the end point
        :param method: Method to call
        :param params: List of parameters
        :return: The result of the method
        :raise RemoteServiceError: Unknown end point / method
        :raise Exception: The exception raised by the method
        """
        # Get the service
        try:
            service = self.__kind_endpoints[kind][name].instance
        except KeyError:
            raise RemoteServiceError("Unknown endpoint: {0}".format(name))

        # Get the method
        method_ref = getattr(service, method, None)
        if method_ref is None:
            raise RemoteServiceError("Unknown method {0}".format(method))

        # Call it (let the errors be propagated)
        return method_ref(*params)

# -----------------------------------------------------------------------------

@ComponentFactory(pelix.remote.FACTORY_REGISTRY_SERVLET)
@Provides(pelix.http.HTTP_SERVLET)
@Provides(pelix.remote.SERVICE_DISPATCHER_SERVLET, "_controller")
@Requires('_dispatcher', pelix.remote.SERVICE_DISPATCHER)
@Requires('_registry', pelix.remote.SERVICE_REGISTRY)
@Property('_path', pelix.http.HTTP_SERVLET_PATH, "/pelix-dispatcher")
class RegistryServlet(object):
    """
    Servlet to access the content of the registry
    """
    def __init__(self):
        """
        Sets up members
        """
        # The framework UID
        self._fw_uid = None

        # The dispatcher
        self._dispatcher = None

        # The imported services registry
        self._registry = None

        # Controller for the provided service:
        # => activate only if bound to a server
        self._controller = False

        # Servlet path property
        self._path = None

        # Ports of exposing servers
        self._ports = []


    def bound_to(self, path, parameters):
        """
        This servlet has been bound to a server

        :param path: The servlet path in the server
        :param parameters: The servlet/server parameters
        """
        port = parameters['http.port']
        if port not in self._ports:
            # Get its access port
            self._ports.append(port)

            # Activate the service, we're bound to a server
            self._controller = True


    def unbound_from(self, path, parameters):
        """
        This servlet has been unbound from a server

        :param path: The servlet path in the server
        :param parameters: The servlet/server parameters
        """
        port = parameters['http.port']
        if port in self._ports:
            # Remove its access port
            self._ports.remove(port)

            # Deactivate the service if no more server available
            if not self._ports:
                self._controller = False


    def do_GET(self, request, response):
        """
        Handles a GET request

        :param request: Request handler
        :param response: Response handler
        """
        # Split the path
        path_parts = request.get_path().split('/')

        if path_parts[-2] == "endpoint":
            # /endpoint/<uid>: specific end point
            uid = path_parts[-1]
            endpoint = self.get_endpoint(uid)
            if endpoint is None:
                response.send_content(404, "Unknown UID: {0}".format(uid),
                                      "text/plain")
                return

            else:
                data = self._make_endpoint_dict(endpoint)

        elif path_parts[-1] == "endpoints":
            # /endpoints: all end points
            endpoints = self.get_endpoints()
            if not endpoints:
                data = []

            else:
                data = [self._make_endpoint_dict(endpoint)
                        for endpoint in endpoints]

        else:
            # Unknown
            response.send_content(404, "Unhandled path", "text/plain")
            return

        # Convert the result to JSON
        data = json.dumps(data)

        # Send the result
        response.send_content(200, data, 'application/json')


    def do_POST(self, request, response):
        """
        Handles a POST request

        :param request: Request handler
        :param response: Response handler
        """
        # Split the path
        path_parts = request.get_path().split('/')

        if path_parts[-1] != "endpoints":
            # Bad path
            response.send_content(404, "Unhandled path", "text/plain")
            return

        # Read the content
        endpoints = json.loads(to_str(request.read_data()))

        if endpoints:
            # Got something
            sender = request.get_client_address()[0]
            for endpoint in endpoints:
                self.register_endpoint(sender, endpoint)

        # We got the end points
        response.send_content(200, 'OK', 'text/plain')


    def _make_endpoint_dict(self, endpoint):
        """
        Converts the end point into a dictionary

        :param endpoint: The end point to convert
        :return: A dictionary
        """
        # Filter the ObjectClass property
        properties = endpoint.get_properties()

        return {"sender": self._fw_uid,
                "uid": endpoint.uid,
                "configurations": endpoint.configurations,
                "name": endpoint.name,
                "specifications": endpoint.specifications,
                "properties": properties}


    def filter_properties(self, framework_uid, properties):
        """
        Replaces in-place export properties by import ones

        :param framework_uid: The UID of the framework exporting the service
        :param properties: End point properties
        :return: The filtered dictionary.
        """
        # Add the "imported" property
        properties[pelix.remote.PROP_IMPORTED] = True

        # Replace the "exported configs"
        if pelix.remote.PROP_EXPORTED_CONFIGS in properties:
            properties[pelix.remote.PROP_IMPORTED_CONFIGS] = \
                                properties[pelix.remote.PROP_EXPORTED_CONFIGS]

        # Clear export properties
        for name in (pelix.remote.PROP_EXPORTED_CONFIGS,
                     pelix.remote.PROP_EXPORTED_INTERFACES):
            if name in properties:
                del properties[name]

        # Add the framework UID to the properties
        properties[pelix.remote.PROP_FRAMEWORK_UID] = framework_uid

        return properties


    def register_endpoint(self, host_address, endpoint_dict):
        """
        Registers a new end point in the registry

        :param host_address: Address of the service exporter
        :param endpoint_dict: An end point description dictionary (result of
                              a request to the dispatcher servlet)
        """
        # Get the UID of the framework exporting the service
        framework = endpoint_dict['sender']

        # Filter properties
        properties = self.filter_properties(framework,
                                            endpoint_dict['properties'])

        # Create the end point object
        endpoint = pelix.remote.beans.ImportEndpoint(endpoint_dict['uid'],
                                                framework,
                                                endpoint_dict['configurations'],
                                                endpoint_dict['name'],
                                                endpoint_dict['specifications'],
                                                properties)

        # Set the host address
        endpoint.server = host_address

        # Register it
        self._registry.add(endpoint)


    def get_access(self):
        """
        Returns the port and path to access this servlet with the first
        bound HTTP service.
        Returns None if this servlet is still not bound to a server

        :return: A tuple: (port, path) or None
        """
        if self._ports:
            return (self._ports[0], self._path)


    def get_endpoints(self):
        """
        Returns the complete list of end points

        :return: The list of all known end points
        """
        return self._dispatcher.get_endpoints()


    def get_endpoint(self, uid):
        """
        Returns the end point with the given UID or None.

        :return: The end point description or None
        """
        return self._dispatcher.get_endpoint(uid)


    def send_discovered(self, host, port, path):
        """
        Sends a "discovered" HTTP POST request to the dispatcher servlet of the
        framework that has been discovered

        :param host: The address of the sender
        :param port: Port of the HTTP server of the sender
        :param path: Path of the dispatcher servlet
        """
        # Get the end points from the dispatcher
        endpoints = [self._make_endpoint_dict(endpoint)
                     for endpoint in self._dispatcher.get_endpoints()]

        # Make the path to /endpoints
        if path[-1] != '/':
            path = path + '/'
        path = urljoin(path, 'endpoints')


        # Request the end points
        try:
            conn = httplib.HTTPConnection(host, port)
            conn.request("POST", path,
                         json.dumps(endpoints),
                         {"Content-Type": "application/json"})

            result = conn.getresponse()
            data = result.read()
            conn.close()

        except Exception as ex:
            _logger.exception("Error accessing a discovered framework: %s", ex)

        else:
            if result.status != 200:
                # Not a valid result
                _logger.warning("Got an HTTP code %d when contacting a "
                                "discovered framework: %s",
                                result.status, data)


    @Invalidate
    def invalidate(self, context):
        """
        Component invalidated
        """
        # Clean up
        self._fw_uid = None


    @Validate
    def validate(self, context):
        """
        Component validated
        """
        # Get the framework UID
        self._fw_uid = context.get_property(pelix.framework.FRAMEWORK_UID)

        _logger.debug("Dispatcher servlet for %s on %s", self._fw_uid,
                      self._path)
