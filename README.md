# 50 First Dates with DeepSeek

A small Python/Streamlit project that gives DeepSeek persistent memory, live tools, and continuity across sessions.

## Why this exists

The DeepSeek desktop experience is powerful, but it has the familiar "50 First Dates" problem: every new session starts from zero. The AI does not remember who you are, what you were building, what it already helped with, or what context mattered yesterday.

So I used DeepSeek API access and a Python script to fix that.

This project gives a DeepSeek-based assistant:

* persistent memory
* searchable conversation history
* basic user continuity
* live web search, weather, and address lookup
* link and YouTube transcript intake
* a more useful long-term assistant experience

## What it does

Instead of treating each conversation as disposable, this app stores useful context and retrieves it later. That lets the assistant behave less like a forgetful chatbot and more like a continuing collaborator.

It also gives the assistant real tools instead of just recall: it can search the web, check weather, look up places, and read the contents of links dropped into the chat when extraction is possible.

The goal is not to create a perfect agent. The goal is practical continuity with useful hands.

## Features

* DeepSeek API integration
* Streamlit browser chat interface
* Local persistent memory using SQLite
* Full-text search over stored conversation history
* Searchable stored context, including past tool results
* Live web search for current or external information
* Weather lookup through Open-Meteo
* Address/place lookup through Nominatim/OpenStreetMap
* Link intelligence for article text extraction
* YouTube transcript extraction when transcripts are available
* SSRF-guarded link fetching that blocks private/internal network ranges
* Local memory export tools
* Conversation continuity across sessions
* Simple Python structure that can be modified and expanded

## Use cases

This is for people who want an AI assistant that remembers the ongoing project, not just the current prompt, and can go check something instead of guessing.

Examples:

* long-running coding projects
* Linux and server troubleshooting
* personal research
* creative worldbuilding
* AI persona experiments
* multi-session debugging
* checking current weather, news, or an address without leaving the chat
* article and video analysis
* "please stop making me explain this again" workflows

## Project philosophy

Most AI chat interfaces forget too much, and most cannot check anything for themselves.

Humans do not collaborate by reintroducing themselves every morning, and they do not rely only on memory when they can look something up. A useful assistant should be able to carry context forward, remember prior work, build on earlier conversations, and verify current facts instead of guessing.

This project is my attempt to make DeepSeek more useful by giving it continuity and a few working tools.

## Installation

Clone the repository:

```bash
git clone https://github.com/sharochka/50-first-dates-with-Deepseek.git
cd 50-first-dates-with-Deepseek
