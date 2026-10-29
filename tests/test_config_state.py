import pytest
from conftest import module


def test_config_token_file_and_url(tmp_path):
    Config = module("config").NativeConfig
    token = tmp_path / "secret"
    token.write_text("synthetic-test-token\n")
    cfg = Config.from_dict(
        {
            "server_url": "https://native.example/prefix/v1/",
            "account_id": "a",
            "token_file": str(token),
        }
    )
    assert cfg.api_url == "https://native.example/prefix/v1"
    assert cfg.load_token() == "synthetic-test-token"
    assert "synthetic-test-token" not in repr(cfg)
    assert cfg.group_reply_mode == "mention"


@pytest.mark.parametrize(
    "change",
    [
        {"server_url": "file:///tmp/api"},
        {"server_url": "https://user:pass@host"},
        {"server_url": "https://host/?token=abc"},
        {"account_id": ""},
        {"group_reply_mode": "maybe"},
        {"allowed_users": "u1,u2"},
        {"enable_private_reply": "false"},
        {"token": "bad\nheader"},
    ],
)
def test_invalid_config_rejected(change):
    data = {"server_url": "http://localhost:6174", "account_id": "a", "token": "fake"}
    data.update(change)
    with pytest.raises(ValueError):
        module("config").NativeConfig.from_dict(data).load_token()


def test_state_deduplicates_atomically_and_survives_restart(tmp_path):
    State = module("state").StateStore
    path = tmp_path / "state.sqlite3"
    state = State(path)
    assert state.cursor == "0"
    assert state.accept("1", "m1", "peer", "900", "queued")
    assert not state.accept("2", "m1", "peer", "900", "queued")
    assert state.cursor == "2"
    state.close()
    state = State(path)
    assert state.cursor == "2"
    assert state.message_status("m1") == "needs_review"
    state.recall("peer", "900", "m1")
    assert state.message_status("m1") == "recalled"
    assert not state.accept("3", "m1", "peer", "900", "queued")
    state.close()


def test_outbox_retains_key_without_content_or_token(tmp_path):
    State = module("state").StateStore
    state = State(tmp_path / "state.sqlite3")
    state.accept("1", "m1", "p", "9", "queued")
    state.begin_send("stable-key", "m1", "p")
    state.finish_send("stable-key", "needs_review", "send-1")
    assert state.send_record("stable-key")["state"] == "needs_review"
    state.close()
    state = State(tmp_path / "state.sqlite3")
    assert state.send_record("stable-key")["send_id"] == "send-1"
    state.close()


def test_server_replacement_never_reuses_old_cursor(tmp_path):
    state_module = module("state")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    state.bind_server("native-server-1")
    state.advance("10000")
    with pytest.raises(state_module.StateIdentityError):
        state.bind_server("native-server-2")
    assert state.cursor == "10000"
    state.close()


def test_reused_local_message_id_is_explicitly_held_for_review(tmp_path):
    state_module = module("state")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    state.accept("1", "m1", "peer", "900", "queued")
    with pytest.raises(state_module.StateIdentityError):
        state.accept("2", "m1", "peer", "901", "queued")
    assert state.cursor == "1"
    assert state.message_status("m1") == "needs_review"
    state.close()


def test_recall_of_old_server_identity_does_not_recall_other_message(tmp_path):
    state = module("state").StateStore(tmp_path / "state.sqlite3")
    state.accept("1", "m1", "peer", "901", "queued")
    state.recall("peer", "900", "m1")
    assert state.message_status("m1") == "queued"
    state.close()


def test_recall_without_local_id_blocks_later_commit_across_restart(tmp_path):
    State = module("state").StateStore
    path = tmp_path / "state.sqlite3"
    state = State(path)
    state.recall("peer", "900", None)
    state.close()
    state = State(path)
    assert not state.accept("2", "late-local-id", "peer", "900", "queued")
    assert state.message_status("late-local-id") == "recalled"
    state.close()
