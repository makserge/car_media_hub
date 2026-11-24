"""The resources API: manifest and files of the charger databases."""
from __future__ import annotations

import hashlib

from car_media_hub import resources_api as RA

from test_regions import AUTH, mgr, root, sync_client  # noqa: F401  (fixtures)


def put(root, name, data=b"SQLite format 3\0abc"):
    d = root / RA.RESOURCES_DIR / "chargers"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return data


async def test_manifest_is_empty_without_the_folder(mgr, tmp_path):
    client = await sync_client(mgr, tmp_path)
    try:
        r = await client.get("/api/resources/manifest", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == {"version": 1, "resources": []}
    finally:
        await client.close()


async def test_manifest_and_file_with_hash(mgr, root, tmp_path):
    data = put(root, "chargers-de.sqlite")
    put(root, "notes.txt")          # not a database name
    put(root, "bad name.sqlite")    # not on the whitelist
    client = await sync_client(mgr, tmp_path)
    try:
        doc = await (await client.get("/api/resources/manifest", headers=AUTH)).json()
        assert doc["resources"] == [{"kind": "chargers", "name": "chargers-de.sqlite", "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()}]
        r = await client.get("/api/resources/files/chargers/chargers-de.sqlite", headers=AUTH)
        assert r.status == 200 and await r.read() == data
        assert r.headers["ETag"] == '"' + hashlib.sha256(data).hexdigest() + '"'
    finally:
        await client.close()


async def test_other_names_and_kinds_are_not_found(mgr, root, tmp_path):
    put(root, "chargers-de.sqlite")
    client = await sync_client(mgr, tmp_path)
    try:
        for path in ("/api/resources/files/chargers/missing.sqlite", "/api/resources/files/chargers/..%2Fx.sqlite",
                     "/api/resources/files/maps/chargers-de.sqlite", "/api/resources/files/chargers/a.txt"):
            r = await client.get(path, headers=AUTH)
            assert r.status == 404, path
    finally:
        await client.close()


async def test_requires_the_token(mgr, tmp_path):
    client = await sync_client(mgr, tmp_path)
    try:
        assert (await client.get("/api/resources/manifest")).status == 401
    finally:
        await client.close()


async def test_changed_file_is_hashed_again(mgr, root, tmp_path):
    put(root, "chargers-de.sqlite", b"one")
    client = await sync_client(mgr, tmp_path)
    try:
        a = await (await client.get("/api/resources/manifest", headers=AUTH)).json()
        put(root, "chargers-de.sqlite", b"twotwo")
        b = await (await client.get("/api/resources/manifest", headers=AUTH)).json()
        assert a["resources"][0]["sha256"] != b["resources"][0]["sha256"]
        assert b["resources"][0]["size"] == 6
    finally:
        await client.close()
