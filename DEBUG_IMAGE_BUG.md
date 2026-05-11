# 🔴 DEBUG: Image Bug Tracking Issue

**Title:** Bot does not respond when messages include images  
**Status:** 🔴 CRITICAL — BLOCKING  
**Date Created:** 2026-05-11  
**Environment:** Railway (n8n + Python Bot)  

---

## Problem Statement

**Symptoms:**
1. User sends text message → Bot replies in 1-2 seconds ✅
2. User sends text + 1-2 images → Bot stays silent 😐
3. No error visible in Discord
4. n8n execution shows: `If node → True branch → Download All Images → [STOPS OR EMPTY]`

**Impact:** Users cannot submit screen time / exam images → cannot get homework

---

## Root Cause Analysis (3 Hypotheses)

### Hypothesis 1: Binary Data Lost in Download All Images Node

**Evidence:**
- n8n execution log shows: "If node Success in 3ms, True Branch (1 item) → item empty"
- Download All Images node does NOT log output
- Binary data never reaches Switch2 Route

**Why This Happens:**
```javascript
const b64List = (() => {
  const raw = $node["Edit Fields1"].json.attachments_b64;
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try { return JSON.parse(raw); } catch { return []; }  // ← SILENT FAILURE
})();
```

If `JSON.parse()` fails, the code silently returns `[]` without logging. The node continues but outputs empty binary.

**Probability:** 🟡 Medium (40%)

---

### Hypothesis 2: Binary Data Incompatible with Switch2 Route

**Evidence:**
- n8n nodes sometimes drop binary data when switching branches
- Switch2 Route has 5 output branches, but binary only preserves on specific connections

**Why This Happens:**
```json
{
  "type": "n8n-nodes-base.switch",
  "parameters": {
    "rules": [
      { "outputKey": "Ba Đăng", ... },
      { "outputKey": "Dứa", ... },
      { "outputKey": "Di", ... },
      { "outputKey": "Whis", ... },
      { "outputKey": "Mẹ Su", ... }
    ]
  }
}
```

If Switch node doesn't have `preserveBinaryData: true`, binary may be lost on branches 2-5.

**Probability:** 🟡 Medium (30%)

---

### Hypothesis 3: Gemini Model Doesn't Support Vision

**Evidence:**
- Using `gemini-3.1-flash-lite` model
- "Lite" and "Flash" variants often have restricted vision capabilities
- Other models (2.5 Flash, Pro Vision) have confirmed vision support

**Why This Happens:**
```json
{
  "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini",
  "parameters": {
    "modelName": "models/gemini-3.1-flash-lite",
    "options": {}
  }
}
```

If this model ID doesn't support `vision/multimodal`, images get silently dropped by Gemini API.

**Probability:** 🟡 Medium (25%)

---

### Hypothesis 4: Body Nesting Level Mismatch (CONFIRMED)

**Evidence:**
- main.py sends 2 levels of `body`: `body.body`
- Workflow expects 3 levels: `body.body.body`
- Edit Fields1 tries to parse: `$json.body.body.body.content` → UNDEFINED

**Why This Happens:**
```python
# main.py current (WRONG)
return {
    "body": {
        "body": {
            "content": message.content,
            # ...
        }
    }
}

# Workflow expects (RIGHT)
{
    "body": {
        "body": {
            "body": {
                "content": message.content,
                # ...
            }
        }
    }
}
```

**Probability:** 🔴 High (80%)

---

## Debugging Roadmap (Step-by-Step)

### Phase 1: Verify Body Nesting Fix (5 minutes)

**Step 1.1: Check current body structure**

In n8n workflow, add a Log node after Webhook:
```json
{
  "type": "n8n-nodes-base.debug",
  "parameters": {
    "message": "Raw webhook input"
  }
}
```

Execute webhook, check logs:
- Look for: `{ "body": { "body": { ... } } }` (2 levels) ❌
- Or: `{ "body": { "body": { "body": { ... } } } }` (3 levels) ✅

**Expected:** Currently 2 levels (broken)

**Step 1.2: Fix main.py**

```python
# main.py line 97-113 — ADD ONE LEVEL
async def build_payload(session: aiohttp.ClientSession,
                        message: discord.Message) -> dict:
    # ... existing code ...
    
    return {
        "body": {
            "body": {
                "body": {  # ← ADD THIS LEVEL
                    "content": message.content,
                    "author": str(message.author.id),
                    "channel_id": str(message.channel.id),
                    "attachments": [...],
                    "attachments_b64": json.dumps(attachments_b64),
                    "discord_activities": json.dumps(get_activities(message)),
                    "referenced_message": {"author_id": ref_author_id} if ref_author_id else None,
                }
            }
        }
    }
```

**Step 1.3: Redeploy & test**

```bash
# Push code to main branch
git add main.py
git commit -m "fix: correct body nesting level (3 instead of 2)"
git push origin main

# Wait for Railway autodeploy (~2 min)
```

Test with text message:
- Expected: Reply within 2-3s ✅

---

### Phase 2: Debug Download All Images Node (10 minutes)

**Step 2.1: Add logging to Download All Images**

In n8n workflow, edit "Download All Images" Code node:

**BEFORE:**
```javascript
const b64List = (() => {
  const raw = $node["Edit Fields1"].json.attachments_b64;
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try { return JSON.parse(raw); } catch { return []; }
})();

if (b64List.length === 0) {
  return [{ json: { ...$node["Edit Fields1"].json } }];
}

const binaries = {};
for (let i = 0; i < b64List.length; i++) {
  const item = b64List[i];
  try {
    const buf  = Buffer.from(item.data, 'base64');
    const mime = item.mime || 'image/png';
    const name = item.name || `screenshot_${i}.png`;
    const key  = i === 0 ? 'data' : `data_${i}`;
    binaries[key] = await this.helpers.prepareBinaryData(buf, name, mime);
  } catch (e) {
    console.log(`Image ${i} error: ${e.message}`);
  }
}

return [{
  json:   { ...$node["Edit Fields1"].json },
  binary: Object.keys(binaries).length > 0 ? binaries : undefined
}];
```

**AFTER (with logging):**
```javascript
const raw = $node["Edit Fields1"].json.attachments_b64;
console.log(`[IMG-DOWNLOAD-START] Raw type: ${typeof raw}, raw value: ${JSON.stringify(raw).substring(0, 100)}`);

const b64List = (() => {
  if (!raw) {
    console.log("[IMG-DOWNLOAD] No raw data, returning []");
    return [];
  }
  
  if (Array.isArray(raw)) {
    console.log(`[IMG-DOWNLOAD] Raw is array with ${raw.length} items`);
    return raw;
  }
  
  try {
    const parsed = JSON.parse(raw);
    console.log(`[IMG-DOWNLOAD] Parsed JSON: ${parsed.length} items`);
    return parsed;
  } catch (e) {
    console.error(`[IMG-DOWNLOAD] JSON.parse() FAILED: ${e.message}`);
    console.error(`[IMG-DOWNLOAD] Raw string: ${raw.substring(0, 200)}`);
    return [];
  }
})();

console.log(`[IMG-DOWNLOAD] b64List.length = ${b64List.length}`);

if (b64List.length === 0) {
  console.log("[IMG-DOWNLOAD] No images, returning JSON only");
  return [{ json: { ...$node["Edit Fields1"].json } }];
}

const binaries = {};
for (let i = 0; i < b64List.length; i++) {
  const item = b64List[i];
  console.log(`[IMG-DOWNLOAD] Processing item ${i}: name=${item.name}, data_length=${item.data?.length || 'MISSING'}`);
  
  try {
    const buf  = Buffer.from(item.data, 'base64');
    const mime = item.mime || 'image/png';
    const name = item.name || `screenshot_${i}.png`;
    const key  = i === 0 ? 'data' : `data_${i}`;
    
    binaries[key] = await this.helpers.prepareBinaryData(buf, name, mime);
    console.log(`[IMG-DOWNLOAD] ✓ Item ${i} OK: ${buf.length} bytes → key="${key}"`);
  } catch (e) {
    console.error(`[IMG-DOWNLOAD] ✗ Item ${i} error: ${e.message}`);
  }
}

console.log(`[IMG-DOWNLOAD] Final binaries keys: [${Object.keys(binaries).join(', ')}]`);
console.log(`[IMG-DOWNLOAD] Returning binary=${binaries.length > 0 ? 'YES' : 'NO'}`);

return [{
  json:   { ...$node["Edit Fields1"].json },
  binary: Object.keys(binaries).length > 0 ? binaries : undefined
}];
```

**Step 2.2: Test with image**

Send message with 1 image to Discord bot:
- Open n8n execution for that message
- Look for logs starting with `[IMG-DOWNLOAD]`
- Check each step:
  - `[IMG-DOWNLOAD-START]` shows raw value ✅
  - `[IMG-DOWNLOAD]` shows items parsed ✅
  - `[IMG-DOWNLOAD]` shows binary keys created ✅

**Expected logs (if working):**
```
[IMG-DOWNLOAD-START] Raw type: string, raw value: "[{"data":"iVBORw0KGgoAAAANSU...","mime":"image/png",...}]"
[IMG-DOWNLOAD] Raw is... (no, it's string)
[IMG-DOWNLOAD] Parsed JSON: 1 items
[IMG-DOWNLOAD] b64List.length = 1
[IMG-DOWNLOAD] Processing item 0: name=screenshot.png, data_length=12345
[IMG-DOWNLOAD] ✓ Item 0 OK: 12345 bytes → key="data"
[IMG-DOWNLOAD] Final binaries keys: [data]
[IMG-DOWNLOAD] Returning binary=YES
```

**If you see:**
```
[IMG-DOWNLOAD] No raw data, returning []
→ raw is undefined or null
→ Check Edit Fields1 node output

[IMG-DOWNLOAD] JSON.parse() FAILED
→ raw is not valid JSON
→ Check if main.py is encoding correctly

[IMG-DOWNLOAD] Final binaries keys: []
→ Buffer.from() or prepareBinaryData() failed
→ Check error message in catch block
```

**Step 2.3: Check Switch2 Route receives binary**

After Download All Images, look at Switch2 Route execution:
- Input should show: `{ json: {...}, binary: { data: [...] } }`
- If binary is missing → Issue is in Download All Images
- If binary exists → Issue is in Switch2 Route or AI agents

---

### Phase 3: Verify Gemini Vision Support (15 minutes)

**Step 3.1: Test in Google AI Studio**

Go to: https://aistudio.google.com/app/apikey

1. Create new chat
2. Select model: `gemini-3.1-flash-lite`
3. Paste system prompt:
   ```
   You are a helpful assistant. Analyze images carefully and describe what you see.
   ```
4. Upload image (screenshot.png)
5. Ask: "Describe this image in detail"

**Expected Result:**
- If model supports vision: Returns detailed description ✅
- If model doesn't support vision: Error like "Model does not support vision" ❌

**If Error:**
→ Use different model: `gemini-2.5-flash` or `gemini-pro-vision`

**Step 3.2: Check n8n workflow model name**

In n8n, open "Google Gemini Chat Model" node:
- Check: `modelName` parameter

Currently set to: `models/gemini-3.1-flash-lite`

If Hypothesis 3 confirmed, change to:
```
models/gemini-2.5-flash
```
or
```
models/gemini-pro-vision
```

**Step 3.3: Test n8n → Gemini with image**

Send image message after model change, check if AI agent output changes.

---

### Phase 4: Verify Switch2 Route (5 minutes)

**Step 4.1: Check binary preservation**

In n8n workflow, edit Switch2 Route node:
- Ensure: All 5 output branches are connected
- Check: If any branch shows "no output" → binary was lost

**Step 4.2: Add preservation flag**

If not present, add to Switch2 Route:
```json
{
  "type": "n8n-nodes-base.switch",
  "parameters": {
    "rules": { ... },
    "options": {
      "passThrough": true  // ← ADD THIS
    }
  }
}
```

---

## Success Criteria

✅ **Bug is FIXED when:**

1. **Message with 2 images sent** to Discord
2. **n8n logs show** `[IMG-DOWNLOAD]` all success (✓ Item 0/1 OK)
3. **Switch2 Route shows** binary data: `{ data: [...], data_1: [...] }`
4. **AI agent receives** image in vision context
5. **Bot replies** within 5-10 seconds with image analysis
6. **User sees** response in Discord ✅

---

## Rollback Plan

If changes cause regression:

```bash
# Revert main.py
git revert <commit-sha>
git push

# Revert n8n workflow
# In n8n: click "Revert to Previous Version" → v14
```

---

## Follow-Up Actions

After bug is fixed:

- [ ] Add unit test for image payload structure
- [ ] Add integration test for full image flow
- [ ] Document image handling in README
- [ ] Monitor message logs for 48h (check for errors)
- [ ] Notify Ba Đăng with summary

---

## Contact & Questions

**Primary Contact:** Ba Đăng (`ddawng.p#0`)  
**Escalation:** If stuck >1 hour → check GitHub discussions or create new issue

