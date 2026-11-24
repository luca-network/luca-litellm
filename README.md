# luca-litellm

Send the cost of every call through your LiteLLM proxy to Luca, so your finance team gets AI spend by customer, team and model, accrued and matched to each provider's invoice, with journal entries for the ERP.

Only the fields accounting needs leave your network: when the call was made, the provider and model, the cost and token counts, and the team, key, project, customer, tags and MCP server LiteLLM records with it. The plugin drops the prompt, the response, tool arguments and tool results before anything is sent. Email addresses are left out unless you turn them on.

## Set up

1. In Luca, add a **LiteLLM AI gateway** connection. Luca shows its webhook URL. Make an ingest token for it:
   `openssl rand -hex 32`
2. On the machine that runs the proxy:
   ```
   pip install luca-litellm
   ```
3. Add the plugin to the proxy's `config.yaml`:
   ```yaml
   litellm_settings:
     callbacks: ["luca_litellm.handler"]
   ```
4. Set two environment variables for the proxy:
   ```
   LUCA_LITELLM_URL=<the webhook URL Luca shows>
   LUCA_LITELLM_TOKEN=<the ingest token>
   ```
5. Check the address and token work. With the two variables set:
   ```
   luca-litellm check
   ```
   It sends Luca one test record, which Luca answers and never counts as usage.
6. Restart the proxy.

The plugin sends calls in batches (every 10 seconds, or every 200 calls), outside the request path, so it adds no time to your calls. If Luca can't be reached, it keeps the calls and tries again.

## Which customer a call served, and what it was for

LiteLLM records a customer for each call when your app passes `user` in the request, or when the call uses a key or team set up for that customer. Luca uses it to show AI cost by customer.

- Send the customer's ID in `user`, as it appears in your ERP's customer list, so cost of revenue can name the customer.
- Tag each request with what it is for, for example `product`, `research` or `internal`. Finance maps each tag to an account.
- Calls with neither go to unallocated AI expense until finance maps them. Luca's month report shows the share of cost that has a function and a customer.

```python
client.chat.completions.create(model="gpt-4o", messages=messages, user="acme-corp",
                               extra_body={"metadata": {"tags": ["product"]}})
```

## Options

| Variable | What it does |
|---|---|
| `LUCA_LITELLM_URL` | The connection's webhook URL (required) |
| `LUCA_LITELLM_TOKEN` | The connection's ingest token (required) |
| `LUCA_LITELLM_INCLUDE_EMAIL` | `true` to send the user's email address with each call (off by default) |

To use it from the LiteLLM Python SDK instead of the proxy:

```python
import litellm
from luca_litellm import LucaLogger
litellm.callbacks = [LucaLogger(url="https://…", token="…")]
```

## Without installing a package

LiteLLM's built-in webhook callback can send the same calls. Luca shows that configuration on the connection too. With it, turn message logging off (`turn_off_message_logging: true`) so prompts and responses are not sent.

Apache-2.0. Questions: hello@luca.network
