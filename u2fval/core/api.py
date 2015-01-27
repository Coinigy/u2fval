# Copyright (c) 2014 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from u2fval.core.controller import U2FController
from u2fval.core.jsobjects import (
    RegisterRequestData, RegisterResponseData, AuthenticateRequestData,
    AuthenticateResponseData)
from u2fval.core.exc import U2fException, BadInputException
from webob.dec import wsgify
from webob import exc, Response
import json
import logging


log = logging.getLogger(__name__)
__all__ = ['create_application']


def u2f_error(e):
    server_e = exc.HTTPBadRequest()
    server_e.body = e.json
    server_e.content_type = 'application/json'
    return server_e


class U2FServerApplication(object):

    def __init__(self, session, memstore, cert_verifier):
        self._session = session
        self._memstore = memstore
        self._cert_verifier = cert_verifier

    @wsgify
    def __call__(self, request):
        client_name = request.environ.get('REMOTE_USER')
        if not client_name:
            raise u2f_error(BadInputException('Client not specified'))
        try:
            resp = self.client(request, client_name)
            if not isinstance(resp, Response):
                resp = Response(json.dumps(resp),
                                content_type='application/json')
            return resp
        except Exception as e:
            self._session.rollback()
            if isinstance(e, U2fException):
                e = u2f_error(e)
            elif isinstance(e, exc.HTTPException):
                pass
            else:
                log.exception('Server error')
                e = exc.HTTPServerError(e.message)
            raise e
        finally:
            self._session.commit()

    def client(self, request, client_name):
        user_id = request.path_info_pop()
        controller = U2FController(self._session, self._memstore, client_name,
                                   self._cert_verifier)
        if not user_id:
            if request.method == 'GET':
                return controller.get_trusted_facets()
            else:
                raise exc.HTTPMethodNotAllowed
        return self.user(request, controller, user_id.encode('utf-8'))

    def user(self, request, controller, user_id):
        if request.path_info_peek():
            page = request.path_info_pop()
            if page == 'register':
                return self.register(request, controller, user_id)
            elif page == 'authenticate':
                return self.authenticate(request, controller, user_id)
            else:
                return self.device(request, controller, user_id, page)

        if request.method == 'GET':
            return controller.get_descriptors(user_id)
        elif request.method == 'DELETE':
            controller.delete_user(user_id)
            return exc.HTTPNoContent()
        else:
            raise exc.HTTPMethodNotAllowed

    def register(self, request, controller, user_id):
        if request.method == 'GET':
            register_requests, sign_requests = controller.register_start(
                user_id)
            return RegisterRequestData(
                registerRequests=register_requests,
                authenticateRequests=sign_requests
            )
        elif request.method == 'POST':
            data = RegisterResponseData(request.body)
            try:
                handle = controller.register_complete(user_id,
                                                      data.registerResponse)
            except KeyError:
                raise exc.HTTPBadRequest
            controller.set_props(handle, data.properties)

            return controller.get_descriptor(user_id, handle)
        else:
            raise exc.HTTPMethodNotAllowed

    def authenticate(self, request, controller, user_id):
        if request.method == 'GET':
            sign_requests = controller.authenticate_start(user_id)
            return AuthenticateRequestData(
                authenticateRequests=sign_requests
            )
        elif request.method == 'POST':
            data = AuthenticateResponseData(request.body)
            try:
                handle = controller.authenticate_complete(
                    user_id, data.authenticateResponse)
            except KeyError:
                raise BadInputException('Malformed request')
            except ValueError as e:
                log.exception('Error in authenticate')
                raise BadInputException(e.message)
            controller.set_props(handle, data.properties)

            return controller.get_descriptor(user_id, handle)
        else:
            raise exc.HTTPMethodNotAllowed

    def device(self, request, controller, user_id, handle):
        try:
            if request.method == 'GET':
                return controller.get_descriptor(user_id, handle)
            elif request.method == 'POST':
                props = json.loads(request.body)
                controller.set_props(handle, props)
                return controller.get_descriptor(user_id, handle)
            elif request.method == 'DELETE':
                controller.unregister(handle)
                return exc.HTTPNoContent()
            else:
                raise exc.HTTPMethodNotAllowed
        except ValueError as e:
            raise exc.HTTPNotFound(e.message)


def create_application(settings):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(settings['db'], echo=False)

    Session = sessionmaker(bind=engine)
    session = Session()

    if settings['disable_attestation']:
        # Dummy verifier that does nothing.
        verifier = lambda x: None
    else:
        from u2fval.attestation.calist import CAListVerifier
        verifier = CAListVerifier()
        for path in settings['ca_certs']:
            verifier.add_ca_dir(path)

    if settings['mc']:
        from u2fval.core.transactionmc import MemcachedStore
        memstore = MemcachedStore(settings['mc_hosts'])
    else:
        from u2fval.core.transactiondb import DBStore
        memstore = DBStore(session)

    return U2FServerApplication(session, memstore, verifier)
