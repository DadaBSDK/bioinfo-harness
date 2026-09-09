"""The hive: per-agent memory, atomic-file mailboxes, a shared blackboard,
an append-only event log, and a searchable memory palace.

Deliberately file-based rather than a daemon or a socket. Every message, fact
and memory is a plain file you can `cat` -- which matters when a run is
half-finished on a cluster node at 3am, and which lets the harness work over
NFS/Lustre where a local socket would not.

Layout under <hive_root>:

    agents/<name>/identity.json   who the agent is, its role and provider
    agents/<name>/memory.md       the agent's own markdown memory
    agents/<name>/inbox/          messages the router delivered here
    agents/<name>/outbox/         messages this agent wrote, awaiting routing
    agents/<name>/archive/        routed messages, kept for provenance
    blackboard/<key>.json         shared facts any agent may read
    events.jsonl                  append-only event log
    palace/index.sqlite           lexical index over all memory + notes
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .schemas import Message, new_id


def atomic_write(path: Path, text: str) -> Path:
    """Write via tmpfile + os.replace so a reader never sees a partial message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


class Mailbox:
    """One agent's inbox/outbox pair."""

    def __init__(self, root: Path, name: str):
        self.name = name
        self.dir = root / "agents" / name
        self.inbox = self.dir / "inbox"
        self.outbox = self.dir / "outbox"
        self.archive = self.dir / "archive"
        for d in (self.inbox, self.outbox, self.archive):
            d.mkdir(parents=True, exist_ok=True)

    def send(self, recipient: str, topic: str, body: dict[str, Any],
             thread: str | None = None) -> Message:
        msg = Message(id=new_id("msg"), sender=self.name, recipient=recipient,
                      topic=topic, body=body, thread=thread)
        atomic_write(self.outbox / f"{msg.ts:.6f}-{msg.id}.json",
                     json.dumps(_msg_dict(msg), indent=2, default=str))
        return msg

    def read(self, consume: bool = True, topic: str | None = None) -> list[Message]:
        msgs: list[Message] = []
        for f in sorted(self.inbox.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue  # a writer is mid-replace; it will be here next poll
            if topic and data.get("topic") != topic:
                continue
            msgs.append(_msg_obj(data))
            if consume:
                f.replace(self.archive / f.name)
        return msgs

    def peek(self, topic: str | None = None) -> list[Message]:
        return self.read(consume=False, topic=topic)

    def pending(self) -> int:
        return len(list(self.inbox.glob("*.json")))


class Blackboard:
    """Shared, last-write-wins facts. The place agents publish conclusions
    that other agents need but should not have to ask for."""

    def __init__(self, root: Path):
        self.dir = root / "blackboard"
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(key: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", key)[:180]

    def put(self, key: str, value: Any, agent: str = "system") -> None:
        atomic_write(self.dir / f"{self._slug(key)}.json", json.dumps(
            {"key": key, "value": value, "agent": agent, "ts": time.time()},
            indent=2, default=str))

    def get(self, key: str, default: Any = None) -> Any:
        f = self.dir / f"{self._slug(key)}.json"
        if not f.exists():
            return default
        try:
            return json.loads(f.read_text())["value"]
        except (json.JSONDecodeError, KeyError, OSError):
            return default

    def entry(self, key: str) -> dict[str, Any] | None:
        f = self.dir / f"{self._slug(key)}.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def keys(self) -> list[str]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(f.read_text())["key"])
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        return out

    def items(self) -> dict[str, Any]:
        return {k: self.get(k) for k in self.keys()}

    def delete(self, key: str) -> bool:
        f = self.dir / f"{self._slug(key)}.json"
        if f.exists():
            f.unlink()
            return True
        return False


class MemoryPalace:
    """Lexical index over per-agent markdown memory and free notes.

    Uses SQLite FTS5 when the build has it, and degrades to LIKE scanning when
    it does not -- the harness must never hard-fail because of a missing
    compile-time SQLite option.
    """

    def __init__(self, root: Path):
        self.dir = root / "palace"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "index.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.fts = self._init_schema()

    def _init_schema(self) -> bool:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY, agent TEXT, kind TEXT, source TEXT,
                text TEXT, tags TEXT, ts REAL)
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS notes_agent ON notes(agent)")
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts "
                "USING fts5(text, agent, kind, content='notes', content_rowid='rowid')")
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            self.conn.commit()
            return False

    def remember(self, text: str, agent: str = "system", kind: str = "note",
                 source: str = "", tags: Iterable[str] = ()) -> str:
        nid = new_id("mem")
        self.conn.execute(
            "INSERT INTO notes (id, agent, kind, source, text, tags, ts) VALUES (?,?,?,?,?,?,?)",
            (nid, agent, kind, source, text, ",".join(tags), time.time()))
        if self.fts:
            row = self.conn.execute("SELECT rowid FROM notes WHERE id=?", (nid,)).fetchone()
            self.conn.execute("INSERT INTO notes_fts (rowid, text, agent, kind) VALUES (?,?,?,?)",
                              (row["rowid"], text, agent, kind))
        self.conn.commit()
        return nid

    def search(self, query: str, limit: int = 10, agent: str | None = None) -> list[dict[str, Any]]:
        if self.fts:
            try:
                sql = ("SELECT n.* FROM notes_fts f JOIN notes n ON n.rowid = f.rowid "
                       "WHERE notes_fts MATCH ?")
                args: list[Any] = [_fts_query(query)]
                if agent:
                    sql += " AND n.agent = ?"
                    args.append(agent)
                sql += " ORDER BY rank LIMIT ?"
                args.append(limit)
                return [dict(r) for r in self.conn.execute(sql, args)]
            except sqlite3.OperationalError:
                pass  # fall through to LIKE
        terms = [t for t in re.split(r"\W+", query.lower()) if t]
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM notes ORDER BY ts DESC")]
        if agent:
            rows = [r for r in rows if r["agent"] == agent]
        scored = [(sum(t in (r["text"] or "").lower() for t in terms), r) for r in rows]
        return [r for s, r in sorted(scored, key=lambda kv: -kv[0]) if s][:limit]

    def index_markdown(self, path: Path, agent: str) -> int:
        """Mine an agent's memory.md into the palace, one heading section per note."""
        if not path.exists():
            return 0
        self.conn.execute("DELETE FROM notes WHERE source=? AND kind='memory'", (str(path),))
        if self.fts:
            self.conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
        chunks, current = [], []
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("#") and current:
                chunks.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            chunks.append("\n".join(current).strip())
        n = 0
        for chunk in chunks:
            if chunk:
                self.remember(chunk, agent=agent, kind="memory", source=str(path))
                n += 1
        return n

    def condense(self, agent: str, keep: int = 200) -> int:
        """Stop the palace growing forever: drop the oldest notes past `keep`."""
        rows = self.conn.execute(
            "SELECT id FROM notes WHERE agent=? AND kind='note' ORDER BY ts DESC", (agent,)
        ).fetchall()
        stale = [r["id"] for r in rows[keep:]]
        for nid in stale:
            self.conn.execute("DELETE FROM notes WHERE id=?", (nid,))
        if stale and self.fts:
            self.conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
        self.conn.commit()
        return len(stale)

    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        per = {r["agent"]: r["c"] for r in self.conn.execute(
            "SELECT agent, COUNT(*) c FROM notes GROUP BY agent")}
        return {"notes": total, "by_agent": per, "fts": self.fts, "db": str(self.db_path)}

    def close(self) -> None:
        self.conn.close()


def _fts_query(query: str) -> str:
    terms = [t for t in re.split(r"\W+", query) if t]
    return " OR ".join(f'"{t}"' for t in terms) or '""'


class Hive:
    """Facade over mailboxes, the blackboard, the palace and the event log."""

    def __init__(self, root: str | Path = ".bioharness/hive"):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "events.jsonl"
        self.blackboard = Blackboard(self.root)
        self.palace = MemoryPalace(self.root)
        self._boxes: dict[str, Mailbox] = {}

    # -- agents ------------------------------------------------------------
    def mailbox(self, name: str) -> Mailbox:
        if name not in self._boxes:
            self._boxes[name] = Mailbox(self.root, name)
        return self._boxes[name]

    def enroll(self, name: str, role: str, provider: str = "builtin",
               **extra: Any) -> dict[str, Any]:
        box = self.mailbox(name)
        identity = {"name": name, "role": role, "provider": provider,
                    "enrolled": time.time(), **extra}
        atomic_write(box.dir / "identity.json", json.dumps(identity, indent=2, default=str))
        mem = box.dir / "memory.md"
        if not mem.exists():
            atomic_write(mem, f"# {name} ({role})\n\nWorking memory for this agent.\n")
        self.event("agent.enrolled", identity)
        return identity

    def roster(self) -> list[dict[str, Any]]:
        out = []
        base = self.root / "agents"
        if not base.exists():
            return out
        for d in sorted(base.iterdir()):
            f = d / "identity.json"
            if f.is_file():
                try:
                    ident = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                ident["inbox_pending"] = self.mailbox(d.name).pending()
                out.append(ident)
        return out

    def append_memory(self, agent: str, text: str, heading: str | None = None) -> None:
        mem = self.mailbox(agent).dir / "memory.md"
        block = (f"\n## {heading}\n\n" if heading else "\n") + text.rstrip() + "\n"
        with open(mem, "a") as fh:
            fh.write(block)
        self.palace.index_markdown(mem, agent)

    # -- routing -----------------------------------------------------------
    def route(self) -> int:
        """Single-router design: only this method moves messages between agents,
        so two agents never race on the same mailbox file."""
        delivered = 0
        base = self.root / "agents"
        if not base.exists():
            return 0
        names = [d.name for d in base.iterdir() if d.is_dir()]
        for sender in names:
            box = self.mailbox(sender)
            for f in sorted(box.outbox.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                targets = [n for n in names if n != sender] \
                    if data.get("recipient") == "*" else [data.get("recipient")]
                for target in filter(None, targets):
                    if target not in names:
                        self.event("message.undeliverable",
                                   {"to": target, "from": sender, "id": data.get("id")})
                        continue
                    atomic_write(self.mailbox(target).inbox / f.name,
                                 json.dumps(data, indent=2, default=str))
                    delivered += 1
                self.event("message.routed", {"from": sender, "to": data.get("recipient"),
                                              "topic": data.get("topic"), "id": data.get("id")})
                f.replace(box.archive / f.name)
        return delivered

    def send(self, sender: str, recipient: str, topic: str,
             body: dict[str, Any], deliver: bool = True) -> Message:
        msg = self.mailbox(sender).send(recipient, topic, body)
        if deliver:
            self.route()
        return msg

    # -- event log ---------------------------------------------------------
    def event(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        line = json.dumps({"ts": time.time(), "event": kind, "payload": payload or {}},
                          default=str)
        with open(self.events_path, "a") as fh:
            fh.write(line + "\n")

    def events(self, kind: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        out = []
        for line in self.events_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is None or rec.get("event") == kind:
                out.append(rec)
        return out[-limit:] if limit else out

    # -- cost ledger -------------------------------------------------------
    def charge(self, agent: str, tokens_in: int = 0, tokens_out: int = 0,
               usd: float = 0.0, model: str = "", note: str = "") -> None:
        self.event("cost.charged", {"agent": agent, "tokens_in": tokens_in,
                                    "tokens_out": tokens_out, "usd": round(usd, 6),
                                    "model": model, "note": note})

    def cost_ledger(self) -> dict[str, Any]:
        rows = [e["payload"] for e in self.events("cost.charged")]
        by_agent: dict[str, dict[str, float]] = {}
        for r in rows:
            acc = by_agent.setdefault(r.get("agent", "?"),
                                      {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "calls": 0})
            acc["tokens_in"] += r.get("tokens_in", 0)
            acc["tokens_out"] += r.get("tokens_out", 0)
            acc["usd"] += r.get("usd", 0.0)
            acc["calls"] += 1
        return {
            "by_agent": {k: {**v, "usd": round(v["usd"], 4)} for k, v in by_agent.items()},
            "total_usd": round(sum(v["usd"] for v in by_agent.values()), 4),
            "total_tokens": sum(v["tokens_in"] + v["tokens_out"] for v in by_agent.values()),
        }

    def status(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "agents": len(self.roster()),
            "pending_messages": sum(a.get("inbox_pending", 0) for a in self.roster()),
            "blackboard_keys": len(self.blackboard.keys()),
            "events": len(self.events()),
            "palace": self.palace.stats(),
            "cost": self.cost_ledger(),
        }


def _msg_dict(m: Message) -> dict[str, Any]:
    return {"id": m.id, "sender": m.sender, "recipient": m.recipient, "topic": m.topic,
            "body": m.body, "thread": m.thread, "ts": m.ts}


def _msg_obj(d: dict[str, Any]) -> Message:
    return Message(id=d.get("id", ""), sender=d.get("sender", ""),
                   recipient=d.get("recipient", ""), topic=d.get("topic", ""),
                   body=d.get("body", {}) or {}, thread=d.get("thread"),
                   ts=float(d.get("ts", 0.0)))
