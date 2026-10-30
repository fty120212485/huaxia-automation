import json

import pytest

from wechat_login_harvester import bot as bot_module
from wechat_login_harvester.bot import (
    UserStore,
    clean_content,
    handle_text,
    parse_command,
    _stop_key,
)


def test_parse_add_command():
    command = parse_command("新增 姓名:张三 身份证:440682198001010011")
    assert command.action == "add"
    assert command.values == {
        "name": "张三",
        "id_card": "440682198001010011",
    }


def test_parse_add_command_with_chinese_colon():
    command = parse_command("新增 姓名：李四 身份证：440682198210063620")
    assert command.action == "add"
    assert command.values["name"] == "李四"
    assert command.values["id_card"] == "440682198210063620"


def test_parse_delete_command():
    command = parse_command("删除 身份证:440682198001010011")
    assert command.action == "delete"
    assert command.target_id == "440682198001010011"


def test_parse_update_command():
    command = parse_command(
        "修改 身份证:旧号码 姓名:新名字 身份证:新号码"
    )
    assert command.action == "update"
    assert command.target_id == "旧号码"
    assert command.values == {"id_card": "新号码", "name": "新名字"}


def test_parse_set_auto_next_command():
    command = parse_command("自动下一门 身份证:id-1 开关:true")
    assert command.action == "set_auto_next"
    assert command.target_id == "id-1"
    assert command.values == {"auto_next_course": True}


def test_parse_set_global_auto_next_command():
    command = parse_command("全局自动下一门 开关:false")
    assert command.action == "set_global_auto_next"
    assert command.values == {"auto_next_course": False}


def test_handle_text_round_trip(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write(
        [
            {"name": "梁敏仪", "id_card": "440682197801283620"},
            {"name": "梁映仪", "id_card": "440682198210063620", "skip": True},
        ]
    )

    assert "新增成功" in handle_text(
        store, "新增 姓名:陈丽梅 身份证:44142319821128074X"
    )
    users = store.read()
    assert len(users) == 3
    assert any(u["id_card"] == "44142319821128074X" for u in users)

    assert "删除成功" in handle_text(
        store, "删除 身份证:440682197801283620"
    )
    assert len(store.read()) == 2

    assert "修改成功" in handle_text(
        store, "修改 身份证:440682198210063620 姓名:梁映芬"
    )
    target = store.query("440682198210063620")[0]
    assert target["name"] == "梁映芬"

    reply = handle_text(store, "查询")
    assert "陈丽梅" in reply
    assert "梁映芬" in reply


def test_handle_text_set_auto_next_updates_account_file(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "张三", "id_card": "id-1"}])
    account_path = tmp_path / "id-1.account"
    account_path.write_text(
        json.dumps({"idCard": "id-1", "autoNextCourse": False}),
        encoding="utf-8",
    )

    reply = handle_text(
        store,
        "自动下一门 身份证:id-1 开关:true",
        account_dir=tmp_path,
    )

    assert "开启" in reply
    payload = json.loads(account_path.read_text(encoding="utf-8"))
    assert payload["autoNextCourse"] is True


def test_handle_text_set_global_auto_next_updates_env(tmp_path, monkeypatch):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "张三", "id_card": "id-1"}])
    env_path = tmp_path / "integration-harness" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("AUTO_NEXT_COURSE=false\n", encoding="utf-8")
    monkeypatch.setattr(bot_module, "INTEGRATION_ENV_PATH", env_path)

    reply = handle_text(store, "全局自动下一门 开关:true")

    assert "开启" in reply
    assert "AUTO_NEXT_COURSE=true" in env_path.read_text(encoding="utf-8")


def test_handle_text_add_duplicate(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "张三", "id_card": "id-1"}])
    reply = handle_text(store, "新增 姓名:李四 身份证:id-1")
    assert "已存在" in reply


def test_user_store_write_is_valid_json(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "张三", "id_card": "id-1", "skip": True}])
    payload = json.loads((tmp_path / "user").read_text(encoding="utf-8"))
    assert payload[0]["skip"] is True
    assert not (tmp_path / "user.tmp").exists()


def test_clean_content_strips_mention():
    assert clean_content("@RobotA 查询") == "查询"
    assert clean_content("查询") == "查询"
    assert clean_content("@RobotA") == ""


class FakeOss:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    def upload_text(self, key: str, content: str) -> None:
        self.uploads.append((key, content))

    def delete_object(self, key: str) -> bool:
        self.deletes.append(key)
        return True


def test_stop_and_resume_commands_write_and_delete_marker(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "张三", "id_card": "id-1"}])
    oss = FakeOss()

    reply = handle_text(store, "停止 身份证:id-1", oss)
    assert "已发送停止指令" in reply
    assert oss.uploads == [(_stop_key("id-1"), "")]

    reply = handle_text(store, "恢复 身份证:id-1", oss)
    assert "已发送恢复指令" in reply
    assert oss.deletes == [_stop_key("id-1")]


def test_stop_command_rejects_unknown_user(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "张三", "id_card": "id-1"}])
    reply = handle_text(store, "停止 身份证:unknown", FakeOss())
    assert "未找到身份证" in reply


@pytest.mark.parametrize("text", [
    "恢复学习 + id-1， id-2,id-1",
    "批量恢复 身份证:id-1, id-2,id-1",
    "批量恢复学习 身份证:id-1,id-2",
    "恢复 身份证：id-1,id-2,id-1",
    "恢复 身份证：id-1， id-2,\nid-1",
])
def test_batch_resume_normalizes_and_deduplicates_ids(tmp_path, text):
    store = UserStore(tmp_path / "user")
    store.write([
        {"name": "张三", "id_card": "id-1"},
        {"name": "李四", "id_card": "id-2"},
    ])
    oss = FakeOss()
    reply = handle_text(store, text, oss)
    assert oss.deletes == [_stop_key("id-1"), _stop_key("id-2")]
    assert "已发送恢复指令 2 个" in reply
    assert "失败" not in reply


@pytest.mark.parametrize("text", ["恢复学习+", "恢复学习+id-1,", "恢复学习+id-1,,id-2", "恢复学习+id-1 id-2", "恢复 身份证：id-1,", "恢复 身份证：id-1,,id-2"])
def test_batch_resume_rejects_malformed_list_without_changes(tmp_path, text):
    oss = FakeOss()
    reply = handle_text(UserStore(tmp_path / "user"), text, oss)
    assert "用逗号分隔" in reply
    assert oss.deletes == []


def test_batch_resume_continues_after_unknown_user_and_oss_failures(tmp_path):
    store = UserStore(tmp_path / "user")
    store.write([{"name": "用户", "id_card": f"id-{n}"} for n in range(1, 5)])

    class PartialOss(FakeOss):
        def delete_object(self, key):
            self.deletes.append(key)
            if key == _stop_key("id-1"):
                return False
            if key == _stop_key("id-2"):
                raise RuntimeError("network error")
            return True

    oss = PartialOss()
    reply = handle_text(store, "恢复学习+id-1,unknown,id-2,id-3,id-4", oss)
    assert oss.deletes == [_stop_key(f"id-{n}") for n in range(1, 5)]
    assert "已发送恢复指令 2 个" in reply
    assert "失败 3 个" in reply
    assert "unknown：未找到用户" in reply
    assert "id-1：删除停止标记失败" in reply
    assert "id-2：network error" in reply


def test_batch_resume_requires_oss(tmp_path):
    reply = handle_text(UserStore(tmp_path / "user"), "恢复学习+id-1,id-2")
    assert reply == "未配置 OSS，无法恢复学习"
