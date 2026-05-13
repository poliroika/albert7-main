"""
Remote MCP server usage via MCPClient.

Demonstrates connecting to a public MCP server (DeepWiki) and using its tools:
  1. Quick start — connect, list tools, call a tool
  2. Calling a tool via the BaseTool interface (execute())
  3. Passing MCP tools into an AgentProfile

No-auth public servers used here:
  - DeepWiki  https://mcp.deepwiki.com/mcp  (Streamable HTTP)
    Tools: ask_question, read_wiki_structure, read_wiki_contents
"""

from gmas.core.agent import AgentProfile

URL = "https://mcp.deepwiki.com/mcp"


def main() -> None:
    try:
        from gmas.tools import MCPClient
    except ImportError as exc:
        print("MCP example skipped: optional dependency is not installed.")
        print(f"Details: {exc}")
        print("Install it with: pip install 'frontier-ai-gmas[mcp]'")
        return

    print("=== 1. Quick start ===")

    with MCPClient(URL) as client:
        tools = client.tools()
        print("Available tools:", [t.name for t in tools])

        answer = client.call_tool(
            "ask_question",
            {
                "repoName": "modelcontextprotocol/python-sdk",
                "question": "How do I create a simple MCP server with a tool?",
            },
        )

        print("\nisError:", answer.isError)
        print("structuredContent:", str(answer.structuredContent)[:300])
        first_text = next((c for c in answer.content if hasattr(c, "text")), None)
        if first_text is not None:
            print("text:", getattr(first_text, "text", "")[:300])

    print("\n=== 2. BaseTool interface ===")

    with MCPClient(URL) as client:
        tools = {t.name: t for t in client.tools()}

        result = tools["ask_question"].execute(
            repoName="modelcontextprotocol/python-sdk",
            question="What transports does the Python MCP SDK support?",
        )

        print("success:", result.success)
        print("output:\n", result.output[:600])
        print("structuredContent:\n", str(result.structured_output)[:300])

    print("\n=== 3. AgentProfile ===")

    client = MCPClient(URL)
    client.connect()

    try:
        agent = AgentProfile(
            agent_id="researcher",
            display_name="Research Agent",
            persona="a helpful research assistant with access to GitHub documentation",
            tools=client.tools(),
        )
        print("Tool names:", agent.get_tool_names())
    finally:
        client.close()


if __name__ == "__main__":
    main()
