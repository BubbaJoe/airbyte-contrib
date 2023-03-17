#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#


from abc import ABC
from datetime import datetime
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple, Union

import requests
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.utils import casing
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.core import IncrementalMixin
from airbyte_cdk.sources.streams.http import HttpStream
from airbyte_cdk.sources.streams.http.auth import TokenAuthenticator
from airbyte_cdk.models.airbyte_protocol import DestinationSyncMode, SyncMode
from google.oauth2 import service_account
import google.auth.transport.requests
import requests
import json

class Helpers(object):
    url_base = "https://firestore.googleapis.com/v1/"
    page_size = 100

    @staticmethod
    def get_collection_path(project_id: str, collection_name: str) -> str:
        return f"projects/{project_id}/databases/(default)/documents:runQuery"

    @staticmethod
    def get_project_url(project_id: str) -> str:
        return f"{Helpers.url_base}projects/{project_id}"

    @staticmethod
    def get_collections_list_url(project_id: str) -> str:
        return f"{Helpers.get_project_url(project_id)}/databases/(default)/documents:listCollectionIds"

    @staticmethod
    def parse_date(date: str) -> datetime:
        return datetime.fromisoformat(date.replace("Z", "+00:00"))

# Basic full refresh stream
class FirestoreStream(HttpStream, ABC):
    _cursor_value: Optional[datetime]
    cursor_field: Union[str, List[str]] = []
    fields: List[str] = []
    @property
    def cursor_key(self):
        if isinstance(self.cursor_field, list):
            if (len(self.cursor_field) > 0):
                return self.cursor_field[0]
            return None
        return self.cursor_field


    url_base = Helpers.url_base
    _primary_key = "name"
    page_size = 100
    http_method = "POST"
    collection_name: str

    @property
    def primary_key(self) -> Optional[Union[str, List[str], List[List[str]]]]:
        return self._primary_key

    @primary_key.setter
    def primary_key(self, value: str) -> None:
        if not isinstance(value, property):
            self._primary_key = value

    @property
    def name(self):
        return casing.camel_to_snake(self.collection_name)

    def __init__(self, authenticator: TokenAuthenticator, collection_name: str):
        super().__init__(
            authenticator=authenticator,
        )
        self._cursor_value = None
        self.collection_name = collection_name

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        documents = list(self.parse_response(response))
        if len(documents) == 0:
            return None
        if self.cursor_key is None:
            return None
        return { "timestampValue": documents[len(documents) - 1][self.cursor_key] }

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        return {}

    def request_body_json(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> Optional[Mapping]:
        timestamp_state: Optional[datetime] = stream_state.get(self.cursor_key) if self.cursor_key else None
        timestamp_value = Helpers.parse_date(timestamp_state).isoformat() if timestamp_state else None

        self.logger.info(f"Requesting body JSON with cursor {self.cursor_key} value {next_page_token}")

        body = {
            "structuredQuery": {
                "from": [{"collectionId": self.collection_name, "allDescendants": True}],
                "where": {
                    "fieldFilter": {
                        "field": { "fieldPath": self.cursor_key },
                        "op": "GREATER_THAN",
                        "value": {
                            "timestampValue": timestamp_value,
                        },
                    }
                } if timestamp_value else None,
                "orderBy": [{"field": {"fieldPath": self.cursor_key}, "direction": "ASCENDING"}],
                "startAt": {"values": [next_page_token], "before": False} if next_page_token else None,
            }
        }

        if self.cursor_key is None or timestamp_value is None:
            del body["structuredQuery"]["where"]
            del body["structuredQuery"]["orderBy"]

        return body

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        data = response.json()
        results = []
        print("GG", data)
        for entry in data:
            if "document" in entry:
                result = {
                    "name": entry["document"]["name"],
                }
                print("GG", entry["document"]["fields"])
                for key, value in dict(entry["document"]["fields"]).items():
                    result[key] = resolve_value(value)
                    objectProp = get_json_schema_type(result[key])
                    self.fields.append((key, objectProp,))

                results.append(result)
        return iter(results)

    def get_json_schema(self) -> Mapping[str, Any]:
        result = {
            "type": "object",
            "$schema": "http://json-schema.org/draft-07/schema#",
            "required": ["name"],
            "properties": {
                "name": { "type": "string" },
            },
            "patternProperties": {
                "^.*$": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {"type": "integer"},
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "array", "items": {"type": "number"}},
                        {"type": "array", "items": {"type": "boolean"}},
                        {"type": "object", "properties": {}}
                    ]
                }
            },
            "additionalProperties": False,
        }
        for (field_key, field_type_def) in self.fields:
            # print("SCHEMA PROP", field_key, field_type_def)
            result["properties"][field_key] = field_type_def
        if self.cursor_key:
            result["properties"][self.cursor_key] = { "type": ["null", "string"] }

        return result


def resolve_value(v: Mapping[str, Any]) -> Any:
    if "arrayValue" in v:
        return [resolve_value(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: resolve_value(v) for k, v in v["mapValue"].get("fields", {}).items()}
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "timestampValue" in v:
        return Helpers.parse_date(v["timestampValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "nullValue" in v:
        return None
    return v

def get_json_schema_type(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        return { "type": "string" }
    if isinstance(value, int):
        return { "type": "integer" }
    if isinstance(value, float):
        return { "type": "number" }
    if isinstance(value, bool):
        return { "type": "boolean" }
    if isinstance(value, list):
        if len(value) == 0:
            return { "type": "array", "items": { "type": "null" } }
        return { "type": "array", "items": get_json_schema_type(value[0]) }
    if isinstance(value, dict):
        return { "type": "object", "properties": { k: get_json_schema_type(v) for k, v in value.items() } }

class IncrementalFirestoreStream(FirestoreStream, IncrementalMixin):
    start_date: Optional[datetime]

    def __init__(self, authenticator: TokenAuthenticator, collection_name: str):
        super().__init__(authenticator=authenticator, collection_name=collection_name)

    @property
    def state(self) -> MutableMapping[str, Any]:
        return { self.cursor_key: self._cursor_value.isoformat() } if self._cursor_value else {}

    @state.setter
    def state(self, value: MutableMapping[str, Any]):
        new_cursor_value = value.get(self.cursor_key, self.start_date)
        self._cursor_value = Helpers.parse_date(new_cursor_value) if isinstance(new_cursor_value, str) else new_cursor_value

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, next_page_token=next_page_token)
        return params

    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: Union[str, List[str]] = None,
        stream_slice: Mapping[str, Any] = None,
        stream_state: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        self.logger.info(f"Stream {self.name}: Reading in {sync_mode} (cursor field {cursor_field}). Current cursor value: {self._cursor_value}")
        self.cursor_field = cursor_field
        for record in super().read_records(
            sync_mode=sync_mode, cursor_field=cursor_field, stream_slice=stream_slice, stream_state=stream_state
        ):
            yield record
            if self.cursor_key:
                record_date = Helpers.parse_date(record[self.cursor_key]) if self.cursor_key else None
                self._cursor_value = max(record_date, self._cursor_value) if self._cursor_value else record_date

class Collection(IncrementalFirestoreStream):
    project_id: str
    collection_name: str

    def __init__(self, authenticator: TokenAuthenticator, collection_name: str, config: Mapping[str, Any]):
        super().__init__(authenticator, collection_name=collection_name)
        self.collection_name = collection_name
        self.project_id = config["project_id"]
        self.start_date = Helpers.parse_date(config["start_date"]) if "start_date" in config else None

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        return Helpers.get_collection_path(self.project_id, self.collection_name)


# Source
class SourceFirestore(AbstractSource):
    def get_auth(self, config: Mapping[str, Any]) -> TokenAuthenticator:
        scopes = ['https://www.googleapis.com/auth/datastore']
        credentials = service_account.Credentials.from_service_account_info(json.loads(config["credentials_json"]), scopes=scopes)        
        credentials.refresh(google.auth.transport.requests.Request())
        return TokenAuthenticator(token=credentials.token)

    def check_connection(self, logger, config: Mapping[str, Any]) -> Tuple[bool, any]:
        auth = self.get_auth(config=config)
        project_id = config["project_id"]
        url = Helpers.get_collections_list_url(project_id)
        try:
            response = requests.get(url, headers=auth.get_auth_header())
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            return False, str(e)
        return True, None

    def discover_collections(self, project_id: str, auth: TokenAuthenticator) -> List[str]:
        url = Helpers.get_collections_list_url(project_id)
        response = requests.post(url, headers=auth.get_auth_header())
        response.raise_for_status()
        json = response.json()
        return json["collectionIds"]

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        auth = self.get_auth(config=config)
        project_id = config["project_id"]
        collections = self.discover_collections(project_id, auth)
        return map(lambda collection_name : Collection(auth, collection_name, config), collections)
