"""Spec import tests: upload, URL import, validation errors, $ref resolution."""

from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

MINIMAL_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Ping API", "version": "2.1.0"},
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "responses": {
                    "200": {
                        "description": "pong",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"pong": {"type": "boolean"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


def _upload(client: TestClient, project_id: str, content: str, filename: str = "openapi.yaml"):
    return client.post(
        f"/projects/{project_id}/specs",
        files={"file": (filename, content, "application/yaml")},
    )


def test_upload_yaml_and_extract_operations(
    client: TestClient, project_id: str, sample_spec: str
) -> None:
    response = _upload(client, project_id, sample_spec)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["openapi_version"] == "3.0.3"
    assert body["version_label"] == "1.4.0"  # defaults to info.version
    assert len(body["content_hash"]) == 64
    assert body["source_url"] is None
    assert body["operation_count"] == 2

    ops = {op["operation_id"]: op for op in body["operations"]}
    assert set(ops) == {"listUsers", "getUser"}
    assert ops["listUsers"]["method"] == "GET"
    assert ops["listUsers"]["path"] == "/users"

    limit = next(p for p in ops["listUsers"]["parameters"] if p["name"] == "limit")
    assert limit["in"] == "query"
    assert limit["required"] is False
    assert limit["example"] == 10

    user_id = ops["getUser"]["parameters"][0]
    assert user_id["name"] == "id"
    assert user_id["in"] == "path"
    assert user_id["required"] is True
    assert user_id["example"] == "u-1"

    # $refs are resolved in the stored operation schemas
    schema_200 = ops["getUser"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema_200["properties"]["address"]["properties"]["zip"]["type"] == "string"
    assert "$ref" not in json.dumps(schema_200)
    assert schema_200["required"] == ["id", "email"]

    array_schema = ops["listUsers"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert array_schema["type"] == "array"
    assert array_schema["items"]["type"] == "object"


def test_upload_response_schema_ref_in_responses(
    client: TestClient, project_id: str, sample_spec: str
) -> None:
    body = _upload(client, project_id, sample_spec).json()
    ops = {op["operation_id"]: op for op in body["operations"]}
    assert "401" in ops["listUsers"]["responses"]
    assert ops["listUsers"]["responses"]["401"]["content"]["application/json"]["schema"][
        "properties"
    ]["message"]


def test_list_specs(client: TestClient, project_id: str, uploaded_spec_id: str) -> None:
    response = client.get(f"/projects/{project_id}/specs")
    assert response.status_code == 200
    specs = response.json()["specs"]
    assert len(specs) == 1
    assert specs[0]["id"] == uploaded_spec_id
    assert specs[0]["operation_count"] == 2
    assert isinstance(specs[0]["operations"], list)


def test_list_specs_empty(client: TestClient, project_id: str) -> None:
    assert client.get(f"/projects/{project_id}/specs").json() == {"specs": []}


@respx.mock
def test_import_from_url(client: TestClient, project_id: str) -> None:
    url = "https://api.example.com/openapi.json"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json=MINIMAL_SPEC,
            headers={"content-type": "application/json"},
        )
    )
    response = client.post(
        f"/projects/{project_id}/specs",
        json={"url": url, "version_label": "v2-baseline"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_url"] == url
    assert body["version_label"] == "v2-baseline"
    assert body["operations"][0]["operation_id"] == "ping"


@respx.mock
def test_import_from_url_http_error(client: TestClient, project_id: str) -> None:
    url = "https://api.example.com/missing.json"
    respx.get(url).mock(return_value=httpx.Response(404))
    response = client.post(f"/projects/{project_id}/specs", json={"url": url})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "upstream_fetch_failed"
    assert "404" in body["error"]["message"]


def test_import_from_url_requires_url_field(client: TestClient, project_id: str) -> None:
    response = client.post(f"/projects/{project_id}/specs", json={"note": "no url"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_invalid"


def test_invalid_response_schema_reports_json_path(client: TestClient, project_id: str) -> None:
    bad = {
        "openapi": "3.0.3",
        "info": {"title": "Bad", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"type": "bogus"}}},
                        }
                    }
                }
            }
        },
    }
    response = _upload(client, project_id, json.dumps(bad), filename="bad.json")
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "spec_invalid"
    assert error["details"], "expected JSON path details"
    assert any(detail["path"].startswith("$.paths") for detail in error["details"]), error[
        "details"
    ]


def test_missing_openapi_field(client: TestClient, project_id: str) -> None:
    response = _upload(client, project_id, "info:\n  title: T\n  version: '1'\npaths: {}\n")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "spec_invalid"
    assert error["details"][0]["path"] == "$.openapi"


def test_unsupported_openapi_version(client: TestClient, project_id: str) -> None:
    response = _upload(
        client,
        project_id,
        "openapi: 2.0\ninfo:\n  title: T\n  version: '1'\npaths: {}\n",
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert "Unsupported OpenAPI version" in error["message"]


def test_openapi_31_supported(client: TestClient, project_id: str) -> None:
    spec_31 = (
        "openapi: 3.1.0\n"
        "info:\n  title: T\n  version: '1'\n"
        "paths:\n"
        "  /ping:\n"
        "    get:\n"
        "      responses:\n"
        "        '200': {description: pong}\n"
    )
    response = _upload(client, project_id, spec_31)
    assert response.status_code == 201, response.text
    assert response.json()["openapi_version"] == "3.1.0"


def test_malformed_document(client: TestClient, project_id: str) -> None:
    response = _upload(client, project_id, "openapi: 3.0.3\ninfo: [unclosed\n")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "spec_invalid"


def test_non_object_document(client: TestClient, project_id: str) -> None:
    response = _upload(client, project_id, "just a string\n")
    assert response.status_code == 422
    assert "object" in response.json()["error"]["message"]


def test_empty_document(client: TestClient, project_id: str) -> None:
    response = _upload(client, project_id, "   \n")
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Document is empty."


def test_unsupported_content_type(client: TestClient, project_id: str) -> None:
    response = client.post(
        f"/projects/{project_id}/specs",
        content=b"openapi: 3.0.3",
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_multipart_requires_file_field(client: TestClient, project_id: str) -> None:
    response = client.post(
        f"/projects/{project_id}/specs",
        files={"spec": ("openapi.yaml", "openapi: 3.0.3", "application/yaml")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_invalid"


def test_external_ref_is_rejected(client: TestClient, project_id: str, sample_spec: str) -> None:
    external = sample_spec.replace(
        '$ref: "#/components/schemas/User"', '$ref: "shared.yaml#/components/schemas/User"'
    )
    response = _upload(client, project_id, external)
    assert response.status_code == 422
    error = response.json()["error"]
    assert "External $ref" in error["message"]
    assert "shared.yaml" in error["message"]
