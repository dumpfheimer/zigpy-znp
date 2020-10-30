import json

import pytest

import zigpy_znp.types as t
import zigpy_znp.commands as c
from zigpy_znp.types.nvids import NWK_NVID_TABLES, NwkNvIds, OsalExNvIds
from zigpy_znp.tools.nvram_read import main as nvram_read
from zigpy_znp.tools.nvram_reset import main as nvram_reset
from zigpy_znp.tools.nvram_write import main as nvram_write

from ..conftest import ALL_DEVICES

pytestmark = [pytest.mark.asyncio]


def not_recognized(req):
    return c.RPCError.CommandNotRecognized.Rsp(
        ErrorCode=c.rpc_error.ErrorCode.InvalidCommandId, RequestHeader=req.header
    )


def dump_nvram(znp):
    obj = {}

    for item_id, items in znp.nvram.items():
        item_id = OsalExNvIds(item_id)
        item = obj[item_id.name] = {}

        for sub_id, value in items.items():
            # Unnamed pass right through
            if item_id != OsalExNvIds.LEGACY:
                item[f"0x{sub_id:04X}"] = value.hex()
                continue

            try:
                # Table entries are named differently
                start, end = next(
                    ((s, e) for s, e in NWK_NVID_TABLES.items() if s <= sub_id <= e)
                )
                item[f"{start.name}+{sub_id - start}"] = value.hex()
            except StopIteration:
                item[NwkNvIds(sub_id).name] = value.hex()

    if znp.nib is not None:
        obj["LEGACY"]["NIB"] = znp.nib.serialize().hex()

    return obj


@pytest.mark.parametrize("device", ALL_DEVICES)
async def test_nvram_read(device, make_znp_server, tmp_path, mocker):
    znp_server = make_znp_server(server_cls=device)

    # Make one reaaally long, requiring multiple writes to read it
    znp_server.nvram[OsalExNvIds.LEGACY][NwkNvIds.HAS_CONFIGURED_ZSTACK3] = (
        b"\xFF" * 300
    )

    # XXX: this is not a great way to do it but deepcopy won't work here
    old_nvram_repr = repr(znp_server.nvram)

    backup_file = tmp_path / "backup.json"
    await nvram_read([znp_server._port_path, "-o", str(backup_file), "-vvv"])

    # No NVRAM was modified during the read
    assert repr(znp_server.nvram) == old_nvram_repr

    # The backup JSON written to disk should be an exact copy
    assert json.loads(backup_file.read_text()) == dump_nvram(znp_server)

    znp_server.close()


@pytest.mark.parametrize("device", ALL_DEVICES)
async def test_nvram_write(device, make_znp_server, tmp_path, mocker):
    znp_server = make_znp_server(server_cls=device)

    # Create a dummy backup
    backup = dump_nvram(znp_server)

    # Change some values
    backup["LEGACY"]["HAS_CONFIGURED_ZSTACK1"] = "ff"

    # Make one with a long value
    backup["LEGACY"]["HAS_CONFIGURED_ZSTACK3"] = "ffee" * 400

    backup_file = tmp_path / "backup.json"
    backup_file.write_text(json.dumps(backup))

    # And clear out all of our NVRAM
    znp_server.nvram = {OsalExNvIds.LEGACY: {}}

    # This has a differing length
    znp_server.nvram[OsalExNvIds.LEGACY][NwkNvIds.HAS_CONFIGURED_ZSTACK1] = b"\xEE\xEE"

    # This already exists
    znp_server.nvram[OsalExNvIds.LEGACY][NwkNvIds.HAS_CONFIGURED_ZSTACK3] = b"\xBB"

    await nvram_write([znp_server._port_path, "-i", str(backup_file)])

    nvram_obj = dump_nvram(znp_server)

    # XXX: should we check that the NVRAMs are *identical*, or that every item in the
    #      backup was completely restored?
    for item_id, sub_ids in backup.items():
        for sub_id, value in sub_ids.items():
            # The NIB is handled differently within tests
            if sub_id == "NIB":
                continue

            assert nvram_obj[item_id][sub_id] == value

    znp_server.close()


@pytest.mark.parametrize("device", ALL_DEVICES)
async def test_nvram_reset_normal(device, make_znp_server, mocker):
    znp_server = make_znp_server(server_cls=device)

    # So we know when it has been changed
    znp_server.nvram[OsalExNvIds.LEGACY][NwkNvIds.STARTUP_OPTION] = b"\xFF"
    znp_server.nvram[OsalExNvIds.LEGACY][0xFFFF] = b"test"

    await nvram_reset([znp_server._port_path])

    # We've instructed Z-Stack to reset on next boot
    assert (
        znp_server.nvram[OsalExNvIds.LEGACY][NwkNvIds.STARTUP_OPTION]
        == (t.StartupOptions.ClearConfig | t.StartupOptions.ClearState).serialize()
    )

    # And none of the "CONFIGURED" values exist
    assert NwkNvIds.HAS_CONFIGURED_ZSTACK1 not in znp_server.nvram[OsalExNvIds.LEGACY]
    assert NwkNvIds.HAS_CONFIGURED_ZSTACK3 not in znp_server.nvram[OsalExNvIds.LEGACY]

    # But our custom value has not been touched
    assert znp_server.nvram[OsalExNvIds.LEGACY][0xFFFF] == b"test"

    znp_server.close()


@pytest.mark.parametrize("device", ALL_DEVICES)
async def test_nvram_reset_everything(device, make_znp_server, mocker):
    znp_server = make_znp_server(server_cls=device)

    await nvram_reset(["-c", znp_server._port_path])

    # Nothing exists but synthetic POLL_RATE_OLD16 in LEGACY
    assert NwkNvIds.POLL_RATE_OLD16 in znp_server.nvram[OsalExNvIds.LEGACY]
    assert len(znp_server.nvram[OsalExNvIds.LEGACY].keys()) == 1
    assert len([v for v in znp_server.nvram.values() if v]) == 1

    znp_server.close()
