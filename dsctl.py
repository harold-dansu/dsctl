#!/usr/bin/env python

# Copyright (c) 2022 Snowplow Analytics Ltd. All rights reserved.
#
# This program is licensed to you under the Apache License Version 2.0,
# and you may not use this file except in compliance with the Apache License Version 2.0.
# You may obtain a copy of the Apache License Version 2.0 at http://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the Apache License Version 2.0 is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the Apache License Version 2.0 for the specific language governing permissions and limitations there under.

import os
from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError, dumps, load
from os.path import join, dirname
import logging
import sys
import argparse
from typing import Dict, Optional

from dotenv import load_dotenv
from requests import get, post, RequestException, Response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)

CONSOLE_HOST = os.environ.get('CONSOLE_HOST', 'console')
CONSOLE_ORGANIZATION_ID = os.environ['CONSOLE_ORGANIZATION_ID']
CONSOLE_API_KEY = os.environ['CONSOLE_API_KEY']

BASE_URL = f"https://{CONSOLE_HOST}.snowplowanalytics.com/api/msc/v1/organizations/{CONSOLE_ORGANIZATION_ID}"
DS_URL = f"{BASE_URL}/data-structures/v1"


@dataclass
class DataStructure:
    vendor: str
    name: str
    format: str


@dataclass
class Version:
    model: int
    revision: int
    addition: int


@dataclass
class Deployment:
    data_structure: DataStructure
    version: Version


class SchemaType(str, Enum):
    EVENT = 'event'
    ENTITY = 'entity'


def get_token() -> Optional[str]:
    """
    Retrieves a JWT from BDP Console.

    :return: The token
    """
    try:
        response = get(
            f"{BASE_URL}/credentials/v2/token",
            headers={"X-API-Key": CONSOLE_API_KEY}
        )
        body = response.json()
        return body["accessToken"]
    except RequestException as e:
        logger.error(f"Could not contact BDP Console: {e}")
    except JSONDecodeError:
        logger.error(f"get_token: Response was not valid JSON: {response.text}")
    except KeyError:
        logger.error(f"get_token: Invalid response body: {dumps(body, indent=2)}")


def get_base_headers(auth_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_token}"
    }


def handle_response(response: Response, action: str) -> bool:
    """
    Generic response handler for validation and promotion operations. Confirms that it all went well.

    :param response: The Response object to operate on
    :param action: The action ('validation' or 'promotion') that created the Response object
    :return: None
    """
    if response.ok:
        try:
            body = response.json()
            if not body['success']:
                logger.error(f"Data structure {action} failed: {body['errors']}")
                return False
            return True
        except JSONDecodeError:
            logger.error(f"handle_response: Response was not valid JSON: {response.text}")
            return False
        except KeyError:
            logger.error(f"handle_response: Invalid response body: {dumps(body, indent=2)}")
            return False
    else:
        logger.error("Data structure {} failed: {}".format(action, response.text))
        return False


def validate(data_structure: dict, auth_token: str, stype: str, contains_meta: bool) -> bool:
    """
    Validates a data structure against the BDP API.

    :param data_structure: A dictionary representing the data structure
    :param auth_token: The JWT to use
    :param stype: The type of the data structure (event or entity)
    :param contains_meta: A flag to indicate whether the `meta` section already exists in the dictionary
    :return:
    """
    if stype not in (SchemaType.EVENT, SchemaType.ENTITY):
        logger.error('Data structure type must be either "event" or "entity"')
        return False

    try:
        response = post(
            f"{DS_URL}/validation-requests",
            json={
                "meta": {
                    "hidden": False,
                    "schemaType": stype,
                    "customData": {}
                },
                "data": data_structure
            } if not contains_meta else data_structure,
            headers=get_base_headers(auth_token)
        )
    except RequestException as e:
        logger.error(f"Could not contact BDP Console: {e}")
        return False

    return handle_response(response, 'validation')


def promote(deployment: Deployment, auth_token, deployment_message: str, to_production=False) -> bool:
    """
    Promotes a data structure to staging or production.

    :param deployment: The Deployment class to use
    :param auth_token: The JWT to use
    :param deployment_message: A message describing the changes applied to the data structure
    :param to_production: A flag to indicate if the data structure should be deployed to production (default: staging)
    :return: None
    """
    try:
        response = post(
            f"{DS_URL}/deployment-requests",
            json={
                "name": deployment.data_structure.name,
                "vendor": deployment.data_structure.vendor,
                "format": deployment.data_structure.format,
                "version": "{}-{}-{}".format(deployment.version.model, deployment.version.revision,
                                             deployment.version.addition),
                "source": "VALIDATED" if not to_production else "DEV",
                "target": "DEV" if not to_production else "PROD",
                "message": deployment_message
            },
            headers=get_base_headers(auth_token)
        )
    except RequestException as e:
        logger.error(f"Could not contact BDP Console: {e}")
        return False

    return handle_response(response, 'promotion')


def resolve(data_structure: dict, includes_meta: bool) -> Optional[Deployment]:
    """
    Reads a data structure and extracts the self-describing section.

    :param data_structure: A dictionary representing the data structure
    :param includes_meta: A flag to indicate whether the `meta` section already exists in the dictionary
    :return: A Deployment instance
    """
    try:
        _self = data_structure['self'] if not includes_meta else data_structure['data']['self']
        vendor = _self['vendor']
        name = _self['name']
        ds_format = _self['format']
        version = _self['version']
        ds = DataStructure(vendor, name, ds_format)
        v = Version(*version.split('-'))
        return Deployment(ds, v)
    except ValueError:
        logger.error("Data structure spec is incorrect: Vendor, name, format or version is invalid")
    except KeyError:
        logger.error("Data structure does not include a correct 'self' element")


def parse_arguments() -> argparse.Namespace:
    """Parses and returns CLI parameters"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--token-only", action="store_true", help="only get an access token and print it on stdout")
    parser.add_argument("--token", type=str, help="use this token to authenticate")
    parser.add_argument("--file", type=str, help="read data structure from file (absolute path) instead of stdin")
    parser.add_argument("--type", choices=('event', 'entity'), help="document type")
    parser.add_argument("--includes-meta", action="store_true",
                        help="the input document already contains the meta field")
    parser.add_argument("--promote-to-dev", action="store_true",
                        help="promote from validated to dev; reads parameters from stdin or --file parameter")
    parser.add_argument("--promote-to-prod", action="store_true",
                        help="promote from dev to prod; reads parameters from stdin or --file parameter")
    parser.add_argument("--message", type=str, help="message to add to version deployment")

    return parser.parse_args()


def parse_input_file(filename: Optional[str]) -> Optional[dict]:
    """
    Loads schema from a file or standard input.

    :param filename: Optional file to read from; if None then use stdin
    :return: The schema JSON
    """
    try:
        if filename:
            with open(filename) as f:
                return load(f)
        else:
            return load(sys.stdin)
    except JSONDecodeError as e:
        logger.error(f"Provided input is not valid JSON: {e}")
    except Exception as e:
        logger.error(f"Could not read {filename if filename else 'stdin'}: {e}")


def flow(args: argparse.Namespace) -> bool:
    """Main operation actually invoking the DS API to validate or promote a data structure"""

    message = args.message if args.message else "No message provided"
    token = args.token if args.token else get_token()
    schema = parse_input_file(args.file)
    schema_type = args.type or "event"
    spec = resolve(schema, args.includes_meta)

    if not token or not schema or not spec:
        return False

    if args.promote_to_dev or args.promote_to_prod:
        return promote(spec, token, message, to_production=True if args.promote_to_prod else False)
    else:
        return validate(schema, token, schema_type, args.includes_meta)


if __name__ == "__main__":
    arguments = parse_arguments()

    if arguments.token_only:
        token = get_token()
        if not token:
            sys.exit(1)
        sys.stdout.write(token)
    else:
        if not flow(arguments):
            sys.exit(1)

    sys.exit(0)
