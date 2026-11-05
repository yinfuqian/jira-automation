# Chatbot Product Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split chatbot product configuration into one file per product and require product then project selection before answering.

**Architecture:** Keep runtime settings in `backend/config/chatbot.json`; load product definitions from `backend/config/products/*.json`; flatten projects into the existing answering pipeline while retaining product identity for two-stage selection.

**Tech Stack:** Python 3, Flask, Redis-backed chat sessions, WeCom long connection, unittest.

---

### Task 1: Product Catalog Loader

**Files:**
- Create: `backend/config/products/*.json`
- Modify: `backend/app/chatbot/settings.py`
- Test: `backend/tests/test_chatbot_gateway.py`

- [ ] Add one JSON file per product and validate duplicate product/project keys and repository mappings.
- [ ] Add `ProductDefinition`, product lookup helpers, and relative project index helpers.
- [ ] Run settings tests.

### Task 2: Two-Stage Conversation State

**Files:**
- Modify: `backend/app/chatbot/session.py`
- Modify: `backend/app/chatbot/wecom/gateway.py`
- Modify: `backend/app/chatbot/routes.py`
- Test: `backend/tests/test_chatbot_gateway.py`

- [ ] Persist `product_key` and `project_key` in Redis.
- [ ] Make the first numeric reply select a product and the second select a project within it.
- [ ] Preserve the pending question until a project is selected.
- [ ] Expose grouped products in `/chatbot/projects`.

### Task 3: Verification and Deployment

**Files:**
- Verify: chatbot settings, gateway, routes, product JSON files.

- [ ] Run focused chat gateway and fault-analysis tests.
- [ ] Validate all product modules have explicit repository URLs.
- [ ] Synchronize changed runtime files to `/data/log-analyzer-master-dev` and restart chatbot services.
