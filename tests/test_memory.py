from mrx.memory import Memory
from mrx.tools import execute


def test_remember_recall_multilingual(memory):
    memory.remember("The user's sister Priya lives in Mumbai", "family")
    memory.remember("Prefers answers in Hindi in the morning")
    memory.remember("उपयोगकर्ता को चाय पसंद है")
    memory.remember("El usuario trabaja en una panadería")
    assert [f["text"] for f in memory.recall("where does Priya live")][0].startswith("The user's sister")
    assert any("चाय" in f["text"] for f in memory.recall("चाय"))
    assert any("panadería" in f["text"] for f in memory.recall("panadería"))
    assert memory.recall("zzzzzz") == [] or all("zzzzzz" not in f["text"] for f in memory.recall("zzzzzz"))


def test_update_and_forget(memory):
    i = memory.remember("old")
    assert memory.update_fact(i, "new")
    assert memory.recall("new")[0]["text"] == "new"
    assert memory.recall("old") == []
    assert memory.forget(i) and not memory.forget(i)


def test_persistence_across_instances(config):
    Memory(config.db_path).remember("persisted fact")
    assert Memory(config.db_path).recall("persisted")[0]["text"] == "persisted fact"


def test_fts_special_characters_do_not_crash(memory):
    memory.remember("C++ and \"quotes\" and AND OR NOT")
    memory.recall('C++ "quotes" AND OR NOT ( ) *')


def test_log_search(memory):
    memory.log("user", "remind me about the dentist")
    memory.log("assistant", "sure")
    assert memory.search_log("dentist")[0]["role"] == "user"
    assert [r["text"] for r in memory.recent_log(5)] == ["remind me about the dentist", "sure"]


def test_drafts_lifecycle(memory):
    d = memory.save_draft("email", "a@b.c", "Hi", "body", {"cc": "x@y.z"})
    assert memory.get_draft(d)["meta"] == {"cc": "x@y.z"}
    memory.set_draft_status(d, "sent")
    assert memory.list_drafts("email")[0]["status"] == "sent"
    assert memory.delete_draft(d) and memory.get_draft(d) is None


def test_devices_upsert_and_label(memory):
    dev = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.5", "hostname": "x", "vendor": "V", "kind": "printer", "open_ports": [80]}
    assert memory.upsert_device(dev) is True
    assert memory.upsert_device({**dev, "ip": "192.168.1.6"}) is False
    assert memory.device(dev["mac"])["ip"] == "192.168.1.6"
    assert memory.label_device(dev["mac"], "Office printer", True)
    d = memory.device(dev["mac"])
    assert d["label"] == "Office printer" and d["trusted"] == 1
    assert not memory.label_device("00:00:00:00:00:01", "x")


def test_tasks(memory):
    t = memory.add_task("n", "p", "every 1h", 100.0)
    assert [x["id"] for x in memory.due_tasks(200.0)] == [t]
    memory.finish_task(t, "done", 400.0)
    assert memory.due_tasks(200.0) == []
    memory.finish_task(t, "done", None)   # one-shot finished -> disabled
    assert memory.tasks()[0]["enabled"] == 0


async def test_memory_tools(ctx):
    r = await execute("memory_save", {"text": "User's birthday is 3 May", "tags": "personal"}, ctx)
    assert not r.is_error
    assert "birthday" in (await execute("memory_search", {"query": "birthday"}, ctx)).content
    assert (await execute("memory_forget", {"id": 999}, ctx)).is_error
