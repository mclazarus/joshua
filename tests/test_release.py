from joshua import release
from joshua.slack import messages as m

SAMPLE = """# Changelog

Intro text, ignored.

## Build 2 · 2026-10-14
- Second thing.
  More about it.

## Build 1 · 2026-10-01 · 7a1fe80
- First thing.
"""


def test_parse_changelog():
    two, one = release.parse_changelog(SAMPLE)
    assert (two.build, two.date, two.commit) == (2, "2026-10-14", None)
    assert two.notes == "- Second thing.\n  More about it."
    assert (one.build, one.date, one.commit, one.notes) == (1, "2026-10-01", "7a1fe80", "- First thing.")


def test_shipped_changelog_counts_up_and_records_commits():
    rs = release.releases()
    assert [r.build for r in rs] == list(range(len(rs), 0, -1))
    assert all(r.notes for r in rs)
    # Every build before the current one has shipped, so its heading names its commit.
    assert all(r.commit for r in rs[1:])
    assert release.find(1) is rs[-1]
    assert release.find(999) is None


def test_commit_prefers_the_git_archive_stamp(tmp_path, monkeypatch):
    (tmp_path / "_commit.txt").write_text("abc1234\n")
    monkeypatch.setattr(release, "HERE", tmp_path)
    release.commit.cache_clear()
    try:
        assert release.commit() == "abc1234"
    finally:
        release.commit.cache_clear()


def test_messages():
    two, one = release.parse_changelog(SAMPLE)
    text, _ = m.version_message(two, None)
    assert "Build 2" in text and "`dev`" in text
    text, blocks = m.changelog_message(one, one.commit, [2, 1])
    assert text == "Changelog for build 1"
    assert "`7a1fe80`" in blocks[0]["text"]["text"]
    assert "• First thing." in blocks[0]["text"]["text"]
    assert "2" in blocks[1]["elements"][0]["text"]
    _, blocks = m.changelog_message(two, None, [2, 1])
    assert blocks[0]["text"]["text"].endswith("• Second thing. More about it.")
    assert "Builds on file: 2, 1" in m.unknown_build("9", [2, 1])
