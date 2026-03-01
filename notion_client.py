import os
import re
import logging
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _clean_notion_id(raw: str) -> str:
    """Accept plain IDs or Notion URLs and return canonical UUID-like form."""
    if not raw:
        return ""
    value = raw.strip().split("?")[0].split("/")[-1]
    value = value.replace("-", "")
    if len(value) == 32:
        return (
            f"{value[0:8]}-{value[8:12]}-{value[12:16]}-"
            f"{value[16:20]}-{value[20:32]}"
        )
    return raw.strip()


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


class NotionClient:
    def __init__(self):
        self.api_key = os.getenv("NOTION_API_KEY", "").strip()
        self.version = os.getenv("NOTION_VERSION", "2022-06-28").strip()
        self.clients_database_id = _clean_notion_id(
            os.getenv("NOTION_CLIENTS_DATABASE_ID", "").strip()
        )
        self.clients_root_page_id = _clean_notion_id(
            os.getenv("NOTION_CLIENTS_ROOT_PAGE_ID", "").strip()
        )
        self.resources_page_id = _clean_notion_id(
            os.getenv("NOTION_RESOURCES_PAGE_ID", "").strip()
        )
        self.timeout = float(os.getenv("NOTION_TIMEOUT_SECONDS", "15"))
        self.max_page_chars = int(os.getenv("NOTION_MAX_PAGE_CHARS", "1800"))
        self.max_context_chars = int(os.getenv("NOTION_MAX_CONTEXT_CHARS", "5000"))

        self._database_title_property_cache: Dict[str, str] = {}

    def is_configured(self) -> bool:
        return bool(self.api_key and (self.clients_database_id or self.clients_root_page_id))

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.version,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        if not self.api_key:
            raise RuntimeError("NOTION_API_KEY is not set")

        url = f"https://api.notion.com/v1/{path.lstrip('/')}"
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=self._headers(),
            json=payload,
            params=params,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            try:
                details = response.json()
            except Exception:
                details = response.text
            raise RuntimeError(f"Notion API error {response.status_code}: {details}")
        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def _rich_text_to_plain(items: List[dict]) -> str:
        return "".join(part.get("plain_text", "") for part in items or [])

    @staticmethod
    def _extract_title_from_page(page: dict) -> str:
        props = page.get("properties", {})
        for _, prop in props.items():
            if prop.get("type") == "title":
                title = NotionClient._rich_text_to_plain(prop.get("title", []))
                if title:
                    return title
        return f"Untitled ({page.get('id', '')[:8]})"

    def _query_database_pages(self, database_id: str, page_size: int = 100) -> List[dict]:
        pages = []
        cursor = None

        while True:
            payload = {"page_size": page_size}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request("POST", f"databases/{database_id}/query", payload=payload)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return pages

    def _list_block_children(self, block_id: str, page_size: int = 100) -> List[dict]:
        blocks = []
        cursor = None

        while True:
            params = {"page_size": page_size}
            if cursor:
                params["start_cursor"] = cursor
            data = self._request("GET", f"blocks/{block_id}/children", params=params)
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return blocks

    def _collect_nested_child_pages(
        self,
        parent_block_id: str,
        depth: int = 0,
        max_depth: int = 3,
        visited: Optional[set] = None,
    ) -> List[dict]:
        if visited is None:
            visited = set()
        if depth > max_depth or parent_block_id in visited:
            return []

        visited.add(parent_block_id)
        found = []
        children = self._list_block_children(parent_block_id)

        for block in children:
            block_id = block.get("id", "")
            block_type = block.get("type", "")

            if block_type == "child_page":
                title = block.get("child_page", {}).get("title", "Untitled")
                found.append({"id": block_id, "title": title, "source": "nested_page"})

            if block.get("has_children"):
                found.extend(
                    self._collect_nested_child_pages(
                        parent_block_id=block_id,
                        depth=depth + 1,
                        max_depth=max_depth,
                        visited=visited,
                    )
                )

        return found

    def list_client_pages(self, max_pages: int = 200) -> List[dict]:
        if not self.is_configured():
            return []

        by_id: Dict[str, dict] = {}

        if self.clients_database_id:
            db_pages = self._query_database_pages(self.clients_database_id)
            for page in db_pages:
                page_id = page.get("id", "")
                if not page_id:
                    continue
                by_id[page_id] = {
                    "id": page_id,
                    "title": self._extract_title_from_page(page),
                    "source": "database",
                }

                nested = self._collect_nested_child_pages(page_id, max_depth=2)
                for child in nested:
                    by_id[child["id"]] = child

        if self.clients_root_page_id:
            nested = self._collect_nested_child_pages(self.clients_root_page_id, max_depth=3)
            for child in nested:
                by_id[child["id"]] = child

        pages = list(by_id.values())
        pages.sort(key=lambda p: p.get("title", "").lower())
        return pages[:max_pages]

    def _select_relevant_pages(self, user_message: str, max_pages: int = 2) -> List[dict]:
        pages = self.list_client_pages()
        if not pages:
            return []

        message = (user_message or "").lower()
        if not message:
            return []

        direct = [page for page in pages if page["title"].lower() in message]
        if direct:
            return direct[:max_pages]

        message_tokens = set(_tokens(user_message))
        scored = []
        for page in pages:
            title_tokens = set(_tokens(page.get("title", "")))
            score = len(message_tokens.intersection(title_tokens))
            if score > 0:
                scored.append((score, page))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [page for _, page in scored[:max_pages]]

    def _block_to_text(self, block: dict) -> str:
        block_type = block.get("type", "")
        data = block.get(block_type, {})

        if block_type in {
            "paragraph",
            "heading_1",
            "heading_2",
            "heading_3",
            "bulleted_list_item",
            "numbered_list_item",
            "to_do",
            "quote",
            "callout",
            "toggle",
        }:
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if not text:
                return ""
            if block_type == "heading_1":
                return f"# {text}"
            if block_type == "heading_2":
                return f"## {text}"
            if block_type == "heading_3":
                return f"### {text}"
            if block_type == "bulleted_list_item":
                return f"- {text}"
            if block_type == "numbered_list_item":
                return f"1. {text}"
            if block_type == "to_do":
                checked = "x" if data.get("checked") else " "
                return f"- [{checked}] {text}"
            if block_type == "quote":
                return f"> {text}"
            return text

        if block_type == "child_page":
            title = data.get("title", "Untitled")
            return f"[Subpage] {title}"

        return ""

    def get_page_excerpt(self, page_id: str, max_chars: Optional[int] = None) -> str:
        limit = max_chars or self.max_page_chars
        blocks = self._list_block_children(page_id)
        lines: List[str] = []
        total = 0

        for block in blocks:
            text = self._block_to_text(block).strip()
            if not text:
                continue
            total += len(text) + 1
            if total > limit:
                break
            lines.append(text)

        excerpt = "\n".join(lines).strip()
        return excerpt[:limit]

    def get_relevant_context(self, user_message: str) -> str:
        if not self.is_configured():
            return ""

        all_pages = self.list_client_pages()
        if not all_pages:
            return ""

        selected = self._select_relevant_pages(user_message, max_pages=2)
        titles = ", ".join(page["title"] for page in all_pages[:20])

        if not selected:
            return f"Known Notion client pages: {titles}"

        sections = [f"Known Notion client pages: {titles}"]
        for page in selected:
            excerpt = self.get_page_excerpt(page["id"])
            if excerpt:
                sections.append(f"### {page['title']}\n{excerpt}")
            else:
                sections.append(f"### {page['title']}\n(No readable text content found.)")

        combined = "\n\n".join(sections)
        return combined[: self.max_context_chars]

    def _get_database_title_property(self, database_id: str) -> str:
        if database_id in self._database_title_property_cache:
            return self._database_title_property_cache[database_id]

        data = self._request("GET", f"databases/{database_id}")
        properties = data.get("properties", {})
        for name, prop in properties.items():
            if prop.get("type") == "title":
                self._database_title_property_cache[database_id] = name
                return name
        raise RuntimeError("Could not find title property on Notion database")

    @staticmethod
    def _text_to_paragraph_blocks(text: str) -> List[dict]:
        if not text:
            return []

        blocks = []
        chunks = []
        for paragraph in text.split("\n\n"):
            para = paragraph.strip()
            if not para:
                continue
            while len(para) > 1800:
                chunks.append(para[:1800])
                para = para[1800:]
            chunks.append(para)

        for chunk in chunks[:50]:
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": chunk}}]
                    },
                }
            )

        return blocks

    def create_client_page(self, title: str, content: str = "") -> str:
        if not self.is_configured():
            raise RuntimeError(
                "Notion is not configured. Set NOTION_API_KEY and NOTION_CLIENTS_DATABASE_ID or NOTION_CLIENTS_ROOT_PAGE_ID."
            )

        blocks = self._text_to_paragraph_blocks(content)

        if self.clients_database_id:
            title_property = self._get_database_title_property(self.clients_database_id)
            payload = {
                "parent": {"database_id": self.clients_database_id},
                "properties": {
                    title_property: {
                        "title": [{"type": "text", "text": {"content": title[:2000]}}]
                    }
                },
            }
        elif self.clients_root_page_id:
            payload = {
                "parent": {"page_id": self.clients_root_page_id},
                "properties": {
                    "title": {
                        "title": [{"type": "text", "text": {"content": title[:2000]}}]
                    }
                },
            }
        else:
            raise RuntimeError("No Notion parent configured for new pages")

        if blocks:
            payload["children"] = blocks

        created = self._request("POST", "pages", payload=payload)
        return created.get("url", "created")

    def fetch_active_clients(self) -> List[dict]:
        """Fetch active clients from the Notion database."""
        if not self.clients_database_id:
            return []
        clients = []
        try:
            pages = self._query_database_pages(self.clients_database_id)
            for page in pages:
                props = page.get("properties", {})
                name, status, goals, notes = "", "", "", ""
                for k, v in props.items():
                    t = v.get("type", "")
                    if t == "title":
                        name = self._rich_text_to_plain(v.get("title", []))
                    elif k.lower() == "status" and t in ("select", "status"):
                        name_val = (v.get("select") or v.get("status") or {}).get("name", "")
                        status = name_val
                    elif k.lower() == "goals":
                        goals = self._rich_text_to_plain(v.get("rich_text", []))
                    elif k.lower() == "notes":
                        notes = self._rich_text_to_plain(v.get("rich_text", []))
                if name and status == "Active":
                    clients.append({"name": name, "goals": goals[:300], "notes": notes[:300]})
        except Exception as e:
            logger.error(f"fetch_active_clients: {e}")
        return clients

    def fetch_existing_resource_titles(self) -> List[str]:
        """List titles of existing resource pages under the resources parent."""
        if not self.resources_page_id:
            return []
        titles = []
        try:
            blocks = self._list_block_children(self.resources_page_id)
            for block in blocks:
                if block.get("type") == "child_page":
                    t = block["child_page"].get("title", "")
                    if t:
                        titles.append(t)
        except Exception as e:
            logger.error(f"fetch_existing_resource_titles: {e}")
        return titles

    def create_resource_page(self, title: str, content: str = "") -> str:
        """Create a new resource page under the resources parent."""
        if not self.resources_page_id:
            raise RuntimeError("NOTION_RESOURCES_PAGE_ID is not set")
        blocks = self._text_to_paragraph_blocks(content)
        payload = {
            "parent": {"page_id": self.resources_page_id},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": title[:2000]}}]
                }
            },
        }
        if blocks:
            payload["children"] = blocks
        created = self._request("POST", "pages", payload=payload)
        return created.get("url", "created")
