# NOTE: you can modify this file as appropriate.

from flask import Blueprint
from flask_restx import Api

v1 = Blueprint("v1", __name__, url_prefix="/api/v1")

_api = Api(
    v1,
    title='Version 1',
    version='1',
    description='The first stable version.',
    authorizations={
        'Bearer': {
            'type': 'apiKey',
            'in': 'header',
            'name': 'Authorization',
            'description': 'JWT Authorization header using Bearer scheme. Example: "Authorization: Bearer {token}"'
        }
    },
    security='Bearer'
)

from .conversations import api as conversations_api
from .updates import api as updates_api

_api.add_namespace(conversations_api)
_api.add_namespace(updates_api)
