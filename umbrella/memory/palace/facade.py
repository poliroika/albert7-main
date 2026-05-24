import os
import pathlib
import time
import uuid
from typing import Any, Iterable, Literal

from umbrella.memory.palace.stores import PalaceStores
from umbrella.memory.palace.tiers import Tier, Scope
from umbrella.memory.palace.recall import RecallBundle
from umbrella.memory.palace.graph import Edge

_POST_PLAN_PHASES = {"plan_review", "execute", "final_review", "verify"}
_PLAN_PROPOSAL_TAGS = {"phase_plan_proposal", "umbrella_plan_candidate"}
_PLAN_DRAFT_TAGS = {"phase_plan", "umbrella_plan"}
_PLAN_SELECTED_TAGS = {"phase_plan_submitted", "umbrella_plan_selected"}

_DEFAULT_CHROMA_STORES = [
    "palace.charter",
    "palace.lesson",
    "palace.idea",
    "palace.codeptr",
    "palace.skill_index",
    "palace.run",
    "palace.phase",
    "palace.subtask",
    "palace.durable",
]


def _palace_root(repo_root: pathlib.Path, workspace_id: str | None) -> pathlib.Path:
    from umbrella.memory.paths import _safe_workspace_segment

    if workspace_id:
        seg = _safe_workspace_segment(workspace_id)
        if seg:
            return repo_root / "workspaces" / seg / ".memory" / "palace"
    return repo_root / ".umbrella" / "palace"


def _provenance_fields_from_meta(meta: dict[str, Any]) -> dict[str, str]:
    return {
        "trust_level": str(meta.get("trust_level") or ""),
        "evidence_refs_json": str(meta.get("evidence_refs_json") or "[]"),
        "verify_run_id": str(meta.get("verify_run_id") or ""),
    }


def _kernel_fields_from_meta(meta: dict[str, Any]) -> dict[str, str]:
    return {
        **_provenance_fields_from_meta(meta),
        "memory_kind": str(meta.get("kind") or meta.get("type") or ""),
        "lifecycle": str(meta.get("lifecycle") or ""),
        "surface": str(meta.get("surface") or ""),
        "source_backend": str(meta.get("source_backend") or ""),
        "external_refs_json": str(meta.get("external_refs_json") or "{}"),
        "metadata_json": str(meta.get("metadata_json") or "{}"),
        "producer": str(meta.get("producer") or ""),
        "agent_kind": str(meta.get("agent_kind") or ""),
    }


def _normalize_store_for_write(
    store: str,
    *,
    kind: str = "",
    tags: list[str] | None = None,
) -> str:
    """palace.global is a logical alias only — never a physical Chroma collection."""
    if store not in {"palace.global", "global", ""}:
        return store
    tag_set = {t.lower() for t in (tags or [])}
    kind_l = str(kind or "").lower()
    if kind_l in {"lesson", "failure_pattern"} or "lesson" in tag_set:
        return "palace.lesson"
    if kind_l in {"codeptr", "code_pointer"} or "codeptr" in tag_set:
        return "palace.codeptr"
    if kind_l in {"skill", "skill_index"} or "skill_index" in tag_set:
        return "palace.skill_index"
    if "failure_pattern" in tag_set:
        return "palace.lesson"
    return "palace.idea"


def _expand_store_aliases(stores: list[str]) -> list[str]:
    expanded: list[str] = []
    for store in stores:
        if store in {"palace.global", "global", ""}:
            expanded.extend(
                ["palace.lesson", "palace.idea", "palace.codeptr", "palace.skill_index"]
            )
        else:
            expanded.append(store)
    seen: set[str] = set()
    ordered: list[str] = []
    for store in expanded:
        if store not in seen:
            seen.add(store)
            ordered.append(store)
    return ordered


class MemPalace:
    def __init__(self, repo_root: pathlib.Path, workspace_id: str | None = None) -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id or ""
        root = _palace_root(repo_root, workspace_id)
        self._stores = PalaceStores(root)

    # ── Write ─────────────────────────────────────────────────────────────

    def add(
        self,
        *,
        store: str,
        content: str,
        tier: str = Tier.WARM,
        scope: str = Scope.RUN_SCOPED,
        tags: Iterable[str] = (),
        phase: str | None = None,
        subtask_id: str | None = None,
        ttl_seconds: int | None = None,
        verified: bool = False,
        source_path: str | None = None,
        run_id: str | None = None,
        links: Iterable[tuple[str, str]] = (),
        extra: dict[str, Any] | None = None,
        kind: str = "",
        node_id: str | None = None,
    ) -> str:
        tags_list = list(tags)
        store = _normalize_store_for_write(
            store, kind=kind or str((extra or {}).get("type", "")), tags=tags_list
        )
        node_id = str(node_id or (extra or {}).get("id") or "").strip() or str(uuid.uuid4())
        metadata: dict[str, Any] = {
            "id": node_id,
            "store": store,
            "tier": tier,
            "scope": scope,
            "tags": ",".join(tags_list),
            "phase": phase or "",
            "subtask_id": subtask_id or "",
            "verified": int(verified),
            "source_path": source_path or "",
            "workspace_id": self._workspace_id,
            "run_id": run_id or "",
            "created_at": time.time(),
        }
        if extra:
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)):
                    metadata[k] = v

        if store == "palace.transient":
            self._stores.transient.add(
                workspace_id=self._workspace_id,
                summary=content[:500],
                body=content if len(content) > 500 else None,
                run_id=run_id,
                phase=phase,
                subtask_id=subtask_id,
                tags=tags_list,
                source_path=source_path,
                ttl_seconds=ttl_seconds,
            )
        else:
            col = self._stores.chroma(store)
            col.add(ids=[node_id], documents=[content], metadatas=[metadata])

        for target_id, edge_type in links:
            self._stores.graph.add_edge(node_id, target_id, edge_type, phase=phase)

        return node_id

    # ── Link ──────────────────────────────────────────────────────────────

    def link(
        self,
        src_id: str,
        dst_id: str,
        edge_type: str,
        weight: float = 1.0,
        phase: str | None = None,
    ) -> None:
        self._stores.graph.add_edge(src_id, dst_id, edge_type, weight=weight, phase=phase)

    # ── List all (no query) ───────────────────────────────────────────────

    def list_all(
        self,
        *,
        stores: list[str] | None = None,
        n: int = 200,
    ) -> list[dict[str, Any]]:
        """Return up to *n* nodes without a semantic query (uses Chroma .get()).

        Prefer this over ``search("")`` when you just want all stored nodes.
        """
        target_stores = _expand_store_aliases(stores or list(_DEFAULT_CHROMA_STORES))
        results: list[dict[str, Any]] = []

        for store in target_stores:
            col = self._stores.chroma(store)
            try:
                res = col.get()
                ids = res.get("ids") or []
                docs = res.get("documents") or ([""] * len(ids))
                metas = res.get("metadatas") or ([{}] * len(ids))
                for i, doc_id in enumerate(ids):
                    meta = metas[i] if i < len(metas) else {}
                    results.append({
                        "id": doc_id,
                        "content": docs[i] if i < len(docs) else "",
                        "store": store,
                        "tier": meta.get("tier", Tier.WARM),
                        "scope": meta.get("scope", ""),
                        "tags": meta.get("tags", ""),
                        "phase": meta.get("phase", ""),
                        "subtask_id": meta.get("subtask_id", ""),
                        "verified": bool(meta.get("verified", 0)),
                        "source_path": meta.get("source_path", ""),
                        "workspace_id": meta.get("workspace_id", self._workspace_id),
                        "run_id": meta.get("run_id", ""),
                        "created_at": meta.get("created_at"),
                        **_kernel_fields_from_meta(meta),
                    })
            except Exception:
                pass

        return results[:n]

    def get(
        self,
        node_id: str,
        *,
        stores: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Return one canonical palace node by id, without semantic search."""
        node_id = str(node_id or "").strip()
        if not node_id:
            return None
        target_stores = _expand_store_aliases(stores or list(_DEFAULT_CHROMA_STORES))
        for store in target_stores:
            if store == "palace.transient":
                continue
            col = self._stores.chroma(store)
            try:
                res = col.get(ids=[node_id])
            except Exception:
                continue
            ids = res.get("ids") or []
            if node_id not in ids:
                continue
            idx = ids.index(node_id)
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            meta = metas[idx] if idx < len(metas) else {}
            return {
                "id": node_id,
                "content": docs[idx] if idx < len(docs) else "",
                "store": store,
                "tier": meta.get("tier", Tier.WARM),
                "scope": meta.get("scope", ""),
                "tags": meta.get("tags", ""),
                "phase": meta.get("phase", ""),
                "subtask_id": meta.get("subtask_id", ""),
                "verified": bool(meta.get("verified", 0)),
                "source_path": meta.get("source_path", ""),
                "workspace_id": meta.get("workspace_id", self._workspace_id),
                "run_id": meta.get("run_id", ""),
                "created_at": meta.get("created_at"),
                **_kernel_fields_from_meta(meta),
            }
        return None

    # ── Search ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        stores: list[str] | None = None,
        tiers: list[str] | None = None,
        scopes: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_all: list[str] | None = None,
        filter_extra: dict[str, Any] | None = None,
        hop: int = 0,
        n: int = 10,
    ) -> list[dict[str, Any]]:
        target_stores = _expand_store_aliases(stores or list(_DEFAULT_CHROMA_STORES))
        results: list[dict[str, Any]] = []
        per_store = max(1, n // len(target_stores) + 1)

        for store in target_stores:
            if store == "palace.transient":
                nodes = self._stores.transient.search_fts(query, n=per_store)
                for node in nodes:
                    results.append({"id": node.id, "content": node.summary, "store": store, "tier": Tier.TRANSIENT})
                continue
            where = self._build_where(tiers=tiers, scopes=scopes)
            col = self._stores.chroma(store)
            try:
                res = col.query(query_texts=[query], n_results=per_store, where=where or None)
                for i, doc_id in enumerate(res["ids"][0]):
                    meta = res["metadatas"][0][i] if res.get("metadatas") else {}
                    if filter_extra and not self._matches_extra_filter(meta, filter_extra):
                        continue
                    if not self._matches_tags(meta, tags_any=tags_any, tags_all=tags_all):
                        continue
                    results.append({
                        "id": doc_id,
                        "content": res["documents"][0][i] if res.get("documents") else "",
                        "store": store,
                        "tier": meta.get("tier", Tier.WARM),
                        "scope": meta.get("scope", ""),
                        "tags": meta.get("tags", ""),
                        "verified": bool(meta.get("verified", 0)),
                        "phase": meta.get("phase", ""),
                        "subtask_id": meta.get("subtask_id", ""),
                        "run_id": meta.get("run_id", ""),
                        "source_path": meta.get("source_path", ""),
                        "workspace_id": meta.get("workspace_id", self._workspace_id),
                        "created_at": meta.get("created_at"),
                        "kind": meta.get("kind", meta.get("type", "")),
                        "score": 1 - (res["distances"][0][i] if res.get("distances") else 0.5),
                        **_kernel_fields_from_meta(meta),
                    })
            except Exception:
                pass

        if hop > 0 and results:
            top_ids = [r["id"] for r in results[:5]]
            for nid in top_ids:
                edges = self._stores.graph.walk(nid, hops=hop, limit=20)
                for edge in edges:
                    neighbour_id = edge.dst_id if edge.src_id == nid else edge.src_id
                    results.append({"id": neighbour_id, "via_edge": edge.edge_type, "store": "graph_walk"})

        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in results:
            if r["id"] not in seen:
                seen.add(r["id"])
                deduped.append(r)

        return deduped[:n]

    @staticmethod
    def _split_tags(raw: Any) -> set[str]:
        if isinstance(raw, str):
            return {tag.strip() for tag in raw.split(",") if tag.strip()}
        if isinstance(raw, (list, tuple, set, frozenset)):
            return {str(tag).strip() for tag in raw if str(tag).strip()}
        return set()

    @staticmethod
    def _rule_store(rule: Any) -> str:
        if isinstance(rule, dict):
            return str(rule.get("store") or "")
        return str(getattr(rule, "store", "") or "")

    @classmethod
    def _rule_tags(cls, rule: Any) -> set[str]:
        if isinstance(rule, dict):
            return cls._split_tags(rule.get("tags") or ())
        return cls._split_tags(getattr(rule, "tags", ()) or ())

    @staticmethod
    def _rule_tier(rule: Any) -> str:
        if isinstance(rule, dict):
            return str(rule.get("tier") or "")
        return str(getattr(rule, "tier", "") or "")

    @staticmethod
    def _rule_n(rule: Any, default: int) -> int:
        raw = rule.get("n") if isinstance(rule, dict) else getattr(rule, "n", None)
        try:
            return max(1, int(raw or default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _rule_filter(rule: Any) -> dict[str, Any] | None:
        raw = rule.get("filter") if isinstance(rule, dict) else getattr(rule, "filter", None)
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _warm_rule_stores(rule: Any) -> list[str]:
        store = MemPalace._rule_store(rule)
        if store in {"palace.global", "global", ""}:
            return ["palace.lesson", "palace.idea", "palace.codeptr", "palace.skill_index"]
        return [store]

    @classmethod
    def _matches_tags(
        cls,
        meta: dict[str, Any],
        *,
        tags_any: list[str] | None = None,
        tags_all: list[str] | None = None,
    ) -> bool:
        node_tags = cls._split_tags(meta.get("tags", ""))
        if tags_any:
            wanted = {t.strip() for t in tags_any if t.strip()}
            if not node_tags.intersection(wanted):
                return False
        if tags_all:
            required = {t.strip() for t in tags_all if t.strip()}
            if not required.issubset(node_tags):
                return False
        return True

    @staticmethod
    def _matches_extra_filter(meta: dict[str, Any], filter_extra: dict[str, Any]) -> bool:
        for key, expected in filter_extra.items():
            if expected is None:
                continue
            key_s = str(key)
            actual = meta.get(key_s)
            if actual is None and key_s == "type":
                actual = meta.get("kind")
            elif actual is None and key_s == "kind":
                actual = meta.get("type")
            if isinstance(expected, (list, tuple, set, frozenset)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _matches_current_run(meta: dict[str, Any], run_id: str | None) -> bool:
        if not run_id:
            return True
        scope = str(meta.get("scope") or "")
        scoped = {
            Scope.RUN_SCOPED,
            Scope.PHASE_SCOPED,
            Scope.SUBTASK_SCOPED,
            "",
        }
        if scope not in scoped:
            return True
        return str(meta.get("run_id") or "") == run_id

    @staticmethod
    def _node_created_at(node: dict[str, Any]) -> float:
        try:
            return float(node.get("created_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _filter_superseded_plan_hot(
        cls,
        phase_id: str,
        run_id: str | None,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep post-plan recall from mixing older plan drafts into prompts."""
        if str(phase_id or "").strip() not in _POST_PLAN_PHASES:
            return candidates
        selected_plan_nodes: list[dict[str, Any]] = []
        for node in candidates:
            if run_id and str(node.get("run_id") or "") != str(run_id):
                continue
            if str(node.get("phase") or "") != "plan":
                continue
            tags = cls._split_tags(node.get("tags", ""))
            if tags & _PLAN_SELECTED_TAGS:
                selected_plan_nodes.append(node)
        latest_selected_id = ""
        if selected_plan_nodes:
            latest_selected = max(selected_plan_nodes, key=cls._node_created_at)
            latest_selected_id = str(latest_selected.get("id") or "")

        filtered: list[dict[str, Any]] = []
        for node in candidates:
            if run_id and str(node.get("run_id") or "") != str(run_id):
                filtered.append(node)
                continue
            if str(node.get("phase") or "") != "plan":
                filtered.append(node)
                continue
            tags = cls._split_tags(node.get("tags", ""))
            if tags & _PLAN_PROPOSAL_TAGS:
                continue
            if latest_selected_id and (tags & _PLAN_SELECTED_TAGS):
                if str(node.get("id") or "") != latest_selected_id:
                    continue
            elif tags & _PLAN_SELECTED_TAGS:
                filtered.append(node)
                continue
            elif tags & _PLAN_DRAFT_TAGS:
                continue
            filtered.append(node)
        return filtered

    def _build_where(
        self, *, tiers: list[str] | None, scopes: list[str] | None
    ) -> dict[str, Any] | None:
        conditions = []
        if tiers and len(tiers) == 1:
            conditions.append({"tier": tiers[0]})
        if scopes and len(scopes) == 1:
            conditions.append({"scope": scopes[0]})
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    # ── Recall ────────────────────────────────────────────────────────────

    def recall(
        self,
        phase_id: str,
        *,
        n: int = 20,
        include_graph: bool = True,
        query_seed: str | None = None,
        run_id: str | None = None,
        always_on_rules: Iterable[Any] | None = None,
        hot_rules: Iterable[Any] | None = None,
        warm_search_rules: Iterable[Any] | None = None,
        graph_policy: Any | None = None,
    ) -> RecallBundle:
        bundle = RecallBundle()
        always_rules = list(always_on_rules or [{"store": "palace.charter", "tier": Tier.ALWAYS_ON}])
        for rule in always_rules:
            store = self._rule_store(rule)
            tier = self._rule_tier(rule) or Tier.ALWAYS_ON
            if not store:
                continue
            col = self._stores.chroma(store)
            try:
                res = col.get(where={"tier": tier})
                for i, doc_id in enumerate(res.get("ids", [])):
                    meta = (res.get("metadatas") or [{}])[i] if res.get("metadatas") else {}
                    bundle.always_on.append({
                        "id": doc_id,
                        "content": res["documents"][i] if res.get("documents") else "",
                        "store": store,
                        "tier": tier,
                        "tags": meta.get("tags", ""),
                        "phase": meta.get("phase", ""),
                        "subtask_id": meta.get("subtask_id", ""),
                        "run_id": meta.get("run_id", ""),
                        "created_at": meta.get("created_at"),
                    })
            except Exception:
                pass

        hot_candidates: list[dict[str, Any]] = []
        hot_policy = list(hot_rules or [{"store": "palace.run", "tags": []}])
        for rule in hot_policy:
            store = self._rule_store(rule)
            allowed_tags = self._rule_tags(rule)
            if not store:
                continue
            col = self._stores.chroma(store)
            recall_tier = Tier.WARM if store == "palace.durable" else Tier.HOT
            try:
                res = col.get(where={"tier": recall_tier})
                ids = res.get("ids") or []
                docs = res.get("documents") or ([""] * len(ids))
                metas = res.get("metadatas") or ([{}] * len(ids))
                for i, doc_id in enumerate(ids):
                    meta = metas[i] if i < len(metas) else {}
                    node_tags = self._split_tags(meta.get("tags", ""))
                    if allowed_tags and not (node_tags & allowed_tags):
                        continue
                    if not self._matches_current_run(meta, run_id):
                        continue
                    hot_candidates.append({
                        "id": doc_id,
                        "content": docs[i] if i < len(docs) else "",
                        "store": store,
                        "tier": recall_tier,
                        "scope": meta.get("scope", ""),
                        "tags": meta.get("tags", ""),
                        "phase": meta.get("phase", ""),
                        "subtask_id": meta.get("subtask_id", ""),
                        "run_id": meta.get("run_id", ""),
                        "created_at": meta.get("created_at") or 0,
                    })
            except Exception:
                pass
        hot_candidates = self._filter_superseded_plan_hot(
            phase_id,
            run_id,
            hot_candidates,
        )
        hot_candidates.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        bundle.hot = hot_candidates[:n]

        if query_seed:
            if warm_search_rules is None:
                warm_policy: list[Any] | None = [
                    {"store": "palace.global", "n": max(1, n // 2)}
                ]
            elif not list(warm_search_rules):
                bundle.warm = []
                warm_policy = None
            else:
                warm_policy = list(warm_search_rules)
            if warm_policy:
                warm_nodes: list[dict[str, Any]] = []
                seen_warm: set[str] = set()
                for rule in warm_policy:
                    warm_tags = list(self._rule_tags(rule))
                    for node in self.search(
                        query_seed,
                        stores=self._warm_rule_stores(rule),
                        tiers=[Tier.WARM],
                        tags_any=warm_tags or None,
                        filter_extra=self._rule_filter(rule),
                        n=self._rule_n(rule, max(1, n // 2)),
                    ):
                        node_id = str(node.get("id") or "")
                        if node_id and node_id in seen_warm:
                            continue
                        if node_id:
                            seen_warm.add(node_id)
                        warm_nodes.append(node)
                bundle.warm = warm_nodes[:n]

        if include_graph and bundle.hot:
            hops = 1
            edge_types: list[str] | None = None
            if graph_policy is not None:
                hops = int(getattr(graph_policy, "hops", None) or 1)
                raw_edges = getattr(graph_policy, "walk_edges", None)
                if raw_edges:
                    edge_types = list(raw_edges)
            top_hot = [n_["id"] for n_ in bundle.hot[:3]]
            graph_limit = 8
            for nid in top_hot:
                edges = self._stores.graph.walk(
                    nid, hops=hops, edge_types=edge_types, limit=20
                )
                for edge in edges:
                    if len(bundle.graph_neighbours) >= graph_limit:
                        break
                    neighbour_id = (
                        edge.dst_id if edge.src_id == nid else edge.src_id
                    )
                    resolved = self.get(neighbour_id)
                    if resolved is None:
                        continue
                    bundle.graph_neighbours.append({
                        "id": neighbour_id,
                        "via_edge": edge.edge_type,
                        "store": str(resolved.get("store") or "graph_walk"),
                        "content": resolved.get("content") or "",
                        "tags": resolved.get("tags", ""),
                        "phase": resolved.get("phase", ""),
                    })
                if len(bundle.graph_neighbours) >= graph_limit:
                    break

        return bundle

    # ── Walk ──────────────────────────────────────────────────────────────

    def walk(
        self,
        node_id: str,
        *,
        edge_types: list[str] | None = None,
        hops: int = 1,
        direction: Literal["in", "out", "both"] = "both",
        limit: int = 50,
    ) -> list[Edge]:
        return self._stores.graph.walk(
            node_id, edge_types=edge_types, hops=hops, direction=direction, limit=limit
        )

    # ── Promote ───────────────────────────────────────────────────────────

    def promote(
        self,
        node_id: str,
        *,
        target_store: str,
        verified: bool = True,
        link_back: bool = True,
        phase: str | None = None,
    ) -> str:
        for store in ["palace.run", "palace.idea", "palace.phase", "palace.subtask"]:
            col = self._stores.chroma(store)
            try:
                res = col.get(ids=[node_id])
                if res.get("ids") and node_id in res["ids"]:
                    content = res["documents"][0] if res.get("documents") else ""
                    meta = res["metadatas"][0] if res.get("metadatas") else {}
                    new_id = self.add(
                        store=_normalize_store_for_write(target_store),
                        content=content,
                        tier=Tier.WARM,
                        scope=Scope.CROSS_RUN_DURABLE,
                        tags=meta.get("tags", "").split(","),
                        verified=verified,
                        phase=meta.get("phase") or phase,
                    )
                    if link_back:
                        self._stores.graph.add_edge(new_id, node_id, "supersedes", phase=phase)
                    return new_id
            except Exception:
                pass
        raise LookupError(f"Node {node_id!r} not found in any mutable store")

    # ── Expire scope ──────────────────────────────────────────────────────

    def expire_scope(self, scope_kind: str, key: str) -> int:
        total = 0
        scope_map = {
            "phase": Scope.PHASE_SCOPED,
            "subtask": Scope.SUBTASK_SCOPED,
            "run": Scope.RUN_SCOPED,
        }
        scope_val = scope_map.get(scope_kind, scope_kind)
        key = str(key or "").strip()
        for store in ["palace.phase", "palace.subtask", "palace.run"]:
            col = self._stores.chroma(store)
            try:
                res = col.get(where={"scope": scope_val})
                ids = res.get("ids") or []
                metas = res.get("metadatas") or []
                to_delete: list[str] = []
                for i, doc_id in enumerate(ids):
                    meta = metas[i] if i < len(metas) else {}
                    if not key:
                        to_delete.append(doc_id)
                        continue
                    if scope_kind == "run" and str(meta.get("run_id") or "") == key:
                        to_delete.append(doc_id)
                    elif scope_kind == "phase" and str(meta.get("phase") or "") == key:
                        to_delete.append(doc_id)
                    elif scope_kind == "subtask" and str(meta.get("subtask_id") or "") == key:
                        to_delete.append(doc_id)
                if to_delete:
                    col.delete(ids=to_delete)
                    total += len(to_delete)
            except Exception:
                pass
        total += self._stores.transient.expire_ttl()
        return total

    # ── Health / stats ────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        return self._stores.health()

    def stats(self) -> dict[str, Any]:
        h = self.health()
        return {"healthy": h["ok"], "stores": h}

    def close(self) -> None:
        self._stores.close()
