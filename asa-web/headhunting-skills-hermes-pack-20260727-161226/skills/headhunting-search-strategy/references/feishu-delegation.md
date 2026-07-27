# Feishu Group Delegation for Liepin Search

When local execution on macOS fails (computer_use unavailable + osascript paste broken + URL search 404), delegate the search task to a worker agent via Feishu group.

## Setup

1. User creates a Feishu group chat containing:
   - Hermes (the strategy agent — you)
   - 二号机 (the worker agent that will execute searches)

2. User provides the group chat ID (format: `oc_<hex>`)

3. Test connectivity:
   ```
   send_message(target="feishu:oc_<chat_id>", message="test")
   ```

## Sending the Strategy

```
send_message(
  target="feishu:oc_<chat_id>",
  message="@二号机 请立即执行搜索...\n\n📎 MEDIA:/path/to/strategy.docx"
)
```

### @Mention Format (Critical)

In Feishu, plain `@name` text does NOT trigger notifications. You must use:
```
<at user_id="open_id">display_name</at>
```

To get a user's `open_id`:
- Use `mcp_feishu_get_feishu_users(queries=[{"query": "二号机"}])` — requires `contact:user.base:readonly` and `contact:user:search` MCP permissions
- If MCP permissions are missing: ask the user to manually @mention the target in the group, then extract the user_id from the message

## Message Content Template

Include in the delegation message:
1. **@mention** the worker agent
2. **Position + Client name**
3. **Strategy file path** (both .md and .docx)
4. **Search plan table**: round number, keywords, filters per round
5. **Key requirements**: hard filters (education, major, experience), pass criteria
6. **Output format**: where to save the results Excel

## Permission Pitfall

`mcp_feishu_get_feishu_users` may fail with a permission error listing missing scopes. Required scopes for user search:
- `contact:user.base:readonly`
- `contact:user:search`

If these are missing, the user needs to add them in the Feishu app console and re-publish. Workaround: have a human manually @mention the target in the group first.
