"""Shared test fixtures.

The database environment variables must be set before any app module is
imported, so they are set at the very top of this file.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="sherlock-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("ALLOW_PRIVATE_HOSTS", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import security  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.main import app  # noqa: E402

SAMPLE_SPEC_YAML = """
openapi: 3.0.3
info:
  title: Users API
  version: 1.4.0
paths:
  /users:
    get:
      operationId: listUsers
      summary: List users
      parameters:
        - name: limit
          in: query
          required: false
          schema:
            type: integer
          example: 10
      responses:
        "200":
          description: A list of users
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: "#/components/schemas/User"
        "401":
          description: unauthorized
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Error"
  /users/{id}:
    get:
      operationId: getUser
      parameters:
        - $ref: "#/components/parameters/UserId"
      responses:
        "200":
          description: A user
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/User"
components:
  parameters:
    UserId:
      name: id
      in: path
      required: true
      schema:
        type: string
      example: "u-1"
  schemas:
    User:
      type: object
      required: [id, email]
      properties:
        id:
          type: string
        email:
          type: string
        address:
          $ref: "#/components/schemas/Address"
    Address:
      type: object
      properties:
        zip:
          type: string
    Error:
      type: object
      properties:
        message:
          type: string
"""


@pytest.fixture(autouse=True)
def allow_public_test_host_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "resolve_target_ips", lambda host, port: ["93.184.216.34"])


@pytest.fixture()
def client() -> TestClient:
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(
        app,
        raise_server_exceptions=False,
        headers={"X-API-Key": os.environ["API_KEY"]},
    ) as test_client:
        yield test_client


@pytest.fixture()
def sample_spec() -> str:
    return SAMPLE_SPEC_YAML


@pytest.fixture()
def project_id(client: TestClient) -> str:
    response = client.post("/projects", json={"name": "Demo"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture()
def uploaded_spec_id(client: TestClient, project_id: str) -> str:
    response = client.post(
        f"/projects/{project_id}/specs",
        files={"file": ("openapi.yaml", SAMPLE_SPEC_YAML, "application/yaml")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]
