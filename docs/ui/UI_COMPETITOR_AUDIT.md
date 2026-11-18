# Official-interface audit for Docket

Research date: 2026-11-18. Scope: official documentation, official interface images, and public first-party navigation source. No competitor was installed, signed into, benchmarked, or tested with private documents. This is an interface review, not a product-quality ranking.

Companions: [Docket IA](DOCKET_INFORMATION_ARCHITECTURE.md), [design system](DOCKET_DESIGN_SYSTEM.md), [screen specifications](DOCKET_SCREEN_SPECIFICATIONS.md).

## Method and evidence limits

**Documented** means stated by the vendor/project. **Observed** means visually inspected in an official image. **Source-confirmed** means read in the project's navigation/component source; this establishes code structure, not shipped acceptance. **Assessment** means my UX inference from that evidence, not user research or a demonstrated defect. **Unknown** means the reviewed sources do not establish it. A documentation site's own table of contents is not treated as the application's sidebar.

Inventories below enumerate the application surfaces established by the reviewed sources, not every possible role-, plan-, plugin-, or version-specific page. Screenshots may trail the current docs. Where labels differ, retain the difference rather than constructing a fictional current interface. No undocumented shortcut or accessible behavior is inferred from a screenshot. Source code is used narrowly to resolve navigation where documentation is incomplete.

First-party source snapshots read through public GitHub API/raw files: OpenJarvis `6656b7fed28e2ff1efa4e77987cf351f2d71eec0`; LightRAG `264b00ebfac646b8366966f4705e36423088dc51`; Khoj `ae229ca894c0b80ad84664afcfdde523b5e87057`; OpenSPG engine `ceeb3ef549df79ca4c4878e7ff452c73584991f3`. These are retrieval identities, not claims about installed releases. Documentation was read live on the research date.

### Visual evidence register

Images were inspected from official origins in temporary research storage; they are linked rather than copied into Docket's repository. Promotional diagrams are explicitly separated from application screenshots.

| Product | Official visual reference | What was actually established |
| --- | --- | --- |
| AnythingLLM | [Annotated chat screenshot](https://raw.githubusercontent.com/Mintplex-Labs/anythingllm-docs/master/public/images/guides/chat-ui.png), [context page](https://docs.anythingllm.com/chat-ui) | Observed workspace/thread sidebar, response actions, source affordance, composer controls; annotation red is documentation markup, not product accent |
| Open WebUI | [Chat screenshot page](https://docs.openwebui.com/features/chat-conversations/chat-features/), [new-chat source image](https://raw.githubusercontent.com/open-webui/docs/main/static/images/features/chat/new-chat-placeholder.png), [rich-answer source image](https://raw.githubusercontent.com/open-webui/docs/main/static/images/features/chat/chat-rich-answer.png) | Visually inspected via official docs repository after docs CDN returned 403: dark sidebar/history, central model/composer entry, rich response/table/diagram, persistent bottom composer |
| Msty Studio | [3.x new-conversation image](https://docs.msty.ai/studio/images/v3/new-conversation-menu.png) | Observed light sidebar, conversation variants menu, lower resource destinations, bottom model/composer controls |
| RAGFlow | [Document/chunk screenshot](https://raw.githubusercontent.com/infiniflow/ragflow-docs/main/images/file_chunks.jpg), [dataset screenshot](https://raw.githubusercontent.com/infiniflow/ragflow-docs/main/images/create_dataset_2.jpg) | Document/chunk image visually inspected: original on left, chunks/actions on right, breadcrumb, pagination; dataset image linked in official quickstart |
| Khoj | [File-filter screenshot](https://raw.githubusercontent.com/khoj-ai/khoj/master/documentation/assets/img/file_filters_conversation.png) | Observed context picker with file search, clear/select all, settings/upload links, file count |
| LightRAG | [Retrieval screenshot](https://raw.githubusercontent.com/HKUDS/LightRAG/main/docs/LightRAG-API-Server.assets/image-20250323123011220.png) | Observed historical v1.2.8/1.2.2 tabbed UI and visible right parameter panel; not assumed current |
| OpenSPG KAG | [Private knowledge guide](https://openspg.yuque.com/ndx6g9/docs_en/dao61dh2gv1ru6zy), [exploration guide](https://openspg.yuque.com/ndx6g9/docs_en/ukrnmmmt5f6guz27), [application guide](https://openspg.yuque.com/ndx6g9/docs_en/rh5hmy19anyb0pga) | Official public guide cover images recovered and inspected after the text reader failed: model configuration/account menu, knowledge exploration table, application list/create dialog. Historical screenshots show different navigation generations |
| OpenJarvis | [Desktop opt-in screenshot](https://raw.githubusercontent.com/open-jarvis/OpenJarvis/main/docs/assets/analytics-opt-in/desktop.jpg) | Observed dark, compact opt-in dialog with cyan primary action and separate sharing choices; does not establish full desktop layout |
| Screenpipe | [Working-set screenshot](https://raw.githubusercontent.com/screenpipe/screenpipe/main/.github/pr-assets/chat-tabs-sidebar/01-working-set.png), [shortcut guide](https://raw.githubusercontent.com/screenpipe/screenpipe/main/.github/pr-assets/chat-tabs-sidebar/06-shortcut-guide.png) | Observed sidebar, chat tabs, recents, composer and keyboard map. Repository PR/test artifacts are development references, not release guarantees |
| Raycast | [Action-panel image](https://2922539984-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2F-Me_8A39tFhZg3UaVoSN%2Fuploads%2Fgit-blob-a1bf254ceb4ad9679111270a8a123946d5538877%2Faction-panel.webp?alt=media&width=800), [official API page](https://developers.raycast.com/api-reference/user-interface/action-panel) | Observed selected default action, grouped secondary actions, keycaps, contextual object title and action search |
| Linear | [Official design-refresh images](https://linear.app/now/behind-the-latest-design-refresh) | Inspected anatomy graphic and actual before/after sidebar: smaller icons, muted inactive items, more vertical padding; app → location bar → view bar → content |

Access notes: the Open WebUI documentation CDN blocked image downloads, so its official source-repository images were used. The KAG guide failed in the text reader; public HTML metadata exposed official cover-image URLs, which were downloaded and inspected. Full guide bodies were not available in that read. Inventory for KAG therefore combines visible screenshot labels with release documentation, without treating the guide's own contents list as the app's navigation. The reviewed screenshots are historical references, not an exhaustive guarantee about every current installation.

## 1. AnythingLLM

**Documented screen/navigation inventory.** Workspaces with nested threads; document attachment/embedding management; workspace settings (including Chat Settings and agent configuration); global Settings including Agent Skills; chat/event logs; Desktop Assistant overlay. The reviewed screenshot confirms workspace/thread nesting and contextual workspace controls. Desktop and self-hosted administration differ; do not merge them into one sidebar. [Chat interface](https://docs.anythingllm.com/chat-ui), [agent setup](https://docs.anythingllm.com/agent/setup), [desktop installation](https://docs.anythingllm.com/installation-desktop/overview).

**Primary workflows.** Add documents to a workspace, choose a chat/query/agent behavior, ask, inspect sources, and revisit a thread. Attached full-text and retrieval-based documents are distinct context paths. [Document use](https://docs.anythingllm.com/chatting-with-documents/introduction), [chat modes](https://docs.anythingllm.com/features/chat-modes).

**Advanced organization and contextual disclosure.** Model/retrieval tuning lives in workspace settings; skills have global configuration and prompt-level toggles. Response operations sit beneath messages; a tool control sits near the composer. This is layered configuration rather than a separate investigation workbench. [Agent setup](https://docs.anythingllm.com/agent/setup), [chat interface](https://docs.anythingllm.com/chat-ui).

**Keyboard.** F1 shows build-specific bindings; Ctrl+, opens settings on Linux/Windows. The documented Ctrl+K opens API Keys, so do not assume it is a universal command palette. Desktop Assistant has its own configurable invocation. [Keyboard shortcuts](https://docs.anythingllm.com/features/keyboard-shortcuts), [Desktop Assistant](https://docs.anythingllm.com/desktop-assistant/introduction).

**Visual hierarchy.** Observed dark neutral workspace tree, large chat reading area, rounded user messages, bottom composer, small response controls, and a sources affordance below output. Workspace controls are contextual rather than standalone main destinations. See visual register.

**Assessment.** Strength: workspace scope and threads give repeated document work a stable container. Weakness for Docket's needs: source inspection and processing health compete with a chat-centered hierarchy, and chat/query/agent distinctions require careful explanation. This is an inference from the documented modes and layout, not a measured failure. Borrow clear source scope and thread context; keep Docket's Library and Evidence independently addressable.

## 2. Open WebUI

**Documented inventory.** Chat/history with folders/projects, side-chat tabs, Workspace sections Models, Knowledge, Prompts, Skills, Tools; Notes, Calendar, Channels; user Settings and administration. Role/permission gates affect availability. Open WebUI Computer is a separate ecosystem product and its file/terminal screens are not silently added to the core inventory. [Workspace](https://docs.openwebui.com/features/workspace/), [chat features](https://docs.openwebui.com/features/chat-conversations/chat-features/).

**Primary workflows.** Select a model; attach files or a reusable knowledge base; ask; inspect output; organize or branch conversations. Knowledge can attach to a chat, folder, or model, making context reusable at several levels. [Knowledge](https://docs.openwebui.com/features/workspace/knowledge/).

**Advanced organization.** Workspace separates reusable assets from conversations. A context-sensitive Create control changes with the selected Workspace section. Model presets bind prompts/knowledge/tools. [Workspace](https://docs.openwebui.com/features/workspace/).

**Context panels/disclosure.** Composer + menu groups attachments and tools; expandable input supports long prompts. Side chats and overview tabs expose secondary work; sidebar hover previews can be disabled. Configuration at chat/account/model levels creates powerful but overlapping controls. [Chat features](https://docs.openwebui.com/features/chat-conversations/chat-features/), [chat parameters](https://docs.openwebui.com/features/chat-conversations/chat-features/chat-params/).

**Keyboard.** Rebindable commands live in Settings > Keyboard; the reviewed chat overview documents Alt+Shift+Left/Right for selected-tab reordering. `#` opens knowledge context selection, `/` invokes prompt templates. Do not import Computer's shortcuts as core WebUI shortcuts. [Chat features](https://docs.openwebui.com/features/chat-conversations/chat-features/), [Knowledge](https://docs.openwebui.com/features/workspace/knowledge/).

**Visual hierarchy.** Observed dark neutral sidebar/history, a model-centered new-chat entry, rounded user prompt blocks, a persistent composer and wide rich-response area. The answer image shows equations, a structured table and diagram. No exact font/spacing tokens were measured. [Official source images](https://github.com/open-webui/docs/tree/main/static/images/features/chat).

**Assessment.** Strength: reusable assets are separated cleanly from per-chat work. Weakness for Docket: context can be attached at several levels, making effective scope harder to explain; broad composer controls risk hiding evidence operations. These are UX inferences from documented configuration paths. Borrow reusable object management and explicit selection chips; keep Docket's current request boundary visible and avoid provider/tool sprawl.

## 3. Msty Studio

**Documented inventory (3.x).** Upper sidebar: conversations/projects and workspace switcher. Lower sidebar: Knowledge Stacks, Prompts, Turnstiles, Skills, Personas, Context Studio, Toolbox, Settings. New Conversation menu adds persona, crew, agent session, Forge Canvas, media session. Model management, environments, and keyboard settings have dedicated documented flows. This is **Studio 3.x**, not legacy Msty App or a merged 2.x feature list. [Find Your Way](https://docs.msty.ai/studio/v3/getting-started/find-your-way).

**Primary workflows.** Start a conversation; choose/connect a model; compare in split chats; create and compose a Knowledge Stack; attach a ready stack to a conversation. [Split chats](https://docs.msty.ai/studio/v3/conversations/split-chat), [Knowledge Stacks](https://docs.msty.ai/studio/v3/knowledge-stacks/overview).

**Advanced organization/disclosure.** Reusable resources have their own hubs. The new-conversation menu reveals alternative work types, while the composer carries model/context controls. Stack source management distinguishes processing from subsequent use in chat. [Source management](https://docs.msty.ai/studio/v3/knowledge-stacks/sources), [Find Your Way](https://docs.msty.ai/studio/v3/getting-started/find-your-way).

**Keyboard.** Ctrl+K on Windows/Linux opens a palette of context-dependent actions/destinations; arrows select, Enter executes, Escape closes. Settings exposes editable bindings and conflict handling. [Keyboard and palette](https://docs.msty.ai/studio/v3/settings/keyboard-shortcuts).

**Visual hierarchy.** Observed light neutral surfaces, thin sidebar divider, sparse conversation canvas, menu groups separated by rules, lower resource navigation and model controls near the composer. This screenshot is model-centered; Docket can make the question and evidence more prominent.

**Assessment.** Strength: reusable knowledge and contextual palette support a broader workspace. Weakness for Docket: names such as Turnstiles, crews, personas, and canvases add a substantial vocabulary before a user can predict the right destination. This is a learnability inference, not usability-test evidence. Borrow contextual commands and resource separation; use plain Docket nouns and one clear question entry.

## 4. RAGFlow

**Documented inventory.** Top navigation includes Dataset, Chat, Agent; FAQs additionally establish Search. Dataset contains file management, configuration, document/chunk inspection, retrieval testing and parsing logs. Agent > Workflow is a canvas with component configuration. User settings includes Model providers. [Quickstart](https://ragflow.io/docs), [dataset creation](https://ragflow.io/docs/v1.0.0/dataset_list_and_creation), [workflow overview](https://ragflow.io/docs/v1.0.0/agent_overview), [FAQ](https://ragflow.io/docs/v1.0.0/faq).

**Primary workflows.** Configure answer/embed models → create dataset → upload/parse → inspect chunks or retrieval → create a chat backed by the dataset → inspect citations. Workflow builders connect steps separately from ordinary Q&A. [Quickstart](https://ragflow.io/docs).

**Advanced organization/disclosure.** Dataset configuration owns parsing; Chat settings own retrieval/generation controls; workflow nodes open a right configuration panel. Advanced groups reveal mode-specific options. [Chat configuration overview](https://ragflow.io/docs/v1.0.0/chat_feature_overview_and_creation), [components](https://ragflow.io/docs/v1.0.0/basic_component).

**Contextual panels.** Observed original-document/chunk split view and chunk-level controls. A failed parsing workflow has logs; retrieval testing separates “not extracted” from “not retrieved.” [Visual register](#visual-evidence-register), [FAQ](https://ragflow.io/docs/v1.0.0/faq).

**Keyboard.** A comprehensive application shortcut inventory was not established from the reviewed official guides. Do not infer keyboard-complete graph/canvas manipulation from pointer instructions.

**Visual hierarchy.** Observed document-first white workspace, breadcrumb and filename header, chunk result list, restrained turquoise status controls, and explicit paging. Not every screen is a card dashboard.

**Assessment.** Strength: exposes the evidence pipeline for diagnosis. Weakness for Docket: dataset, chat app, and workflow setup introduce several preparation steps before a simple personal-library question; citations are an advanced setting in the quickstart. Borrow inspectable chunks and processing diagnostics, but make citations normal and avoid asking Docket users to build a retrieval application.

## 5. Khoj

**Documented/source-confirmed inventory.** Web navigation source lists Home, Agents, Automations, Search, Settings; New Chat and conversation history sit alongside these. Document filters are part of conversation context. Admin configuration is a separate server interface, not another normal user tab. [Pinned sidebar source](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/interface/web/app/components/appSidebar/appSidebar.tsx), [features](https://docs.khoj.dev/category/features/), [admin](https://docs.khoj.dev/advanced/admin/).

**Primary workflows.** Search notes/documents, ask with file filters, select an agent, schedule an automation, or invoke desktop Mini with copied context. [Chat](https://docs.khoj.dev/features/chat/), [Mini](https://docs.khoj.dev/features/khoj_mini/).

**Advanced organization/disclosure.** Agents and Automations are separate destinations; slash commands change the question's mode/context. Host model configuration lives in administration. File filters are exposed in a contextual conversation panel. [Chat](https://docs.khoj.dev/features/chat/), [admin](https://docs.khoj.dev/advanced/admin/).

**Context panels.** Observed Manage Context picker shows file search, count, clear/select all, settings and upload links. It makes selected material visible without a permanent file-management page occupying the conversation.

**Keyboard.** Mini uses Ctrl/Cmd+Shift+K and inserts copied text for editing before send. The shortcut documentation's Up/Down input-history behavior is specifically under Obsidian; it is not evidence for identical web behavior. [Mini](https://docs.khoj.dev/features/khoj_mini/), [keyboard documentation](https://docs.khoj.dev/features/keyboard_shortcuts/).

**Visual hierarchy.** The inspected context menu is a compact light popover with group labels and ordinary text actions; broader web branding is not inferred from that crop.

**Assessment.** Strength: dedicated Search and visible context selection provide alternatives to prompting. Weakness for Docket: slash modes mixing notes, world knowledge and online research could obscure the evidence boundary; duplicated server/user settings require clear ownership. These are inferences from the documented flows. Borrow context visibility and direct search; keep Docket library-only answering explicit.

## 6. LightRAG

**Source-confirmed inventory.** Admin WebUI has Documents, Knowledge Graph, Retrieval; API documentation is linked separately. A historical screenshot labels API as a fourth tab; current `App.tsx` mounts three content tabs. A newer query-only `/workspace` entry intentionally excludes management/graph surfaces. [Pinned app source](https://github.com/HKUDS/LightRAG/blob/264b00ebfac646b8366966f4705e36423088dc51/lightrag_webui/src/App.tsx), [header](https://github.com/HKUDS/LightRAG/blob/264b00ebfac646b8366966f4705e36423088dc51/lightrag_webui/src/features/SiteHeader.tsx), [release notes](https://github.com/HKUDS/LightRAG/releases).

**Primary workflows.** Manage/index documents, explore entity relationships, query with a retrieval mode, inspect outputs. Distinct admin and query-only entries adapt complexity to audience. [Official server guide](https://github.com/HKUDS/LightRAG/blob/main/docs/LightRAG-API-Server.md), [deployment entries](https://github.com/HKUDS/LightRAG/blob/main/docs/MultiSiteDeployment.md).

**Advanced organization/disclosure.** Graph controls include search, labels, layouts, zoom, settings and selected properties. Query parameters are a separate right-hand region in the inspected historical image. [Pinned graph viewer](https://github.com/HKUDS/LightRAG/blob/264b00ebfac646b8366966f4705e36423088dc51/lightrag_webui/src/features/GraphViewer.tsx).

**Context panels.** Graph property inspection and retrieval parameter panels keep object detail beside the active work. The image exposes many token/retrieval fields at once; do not assume every field remains visible in current versions.

**Keyboard.** No comprehensive user-facing global shortcut map established in reviewed guides/source. Keyboard equivalence of graph controls remains unverified.

**Visual hierarchy.** Observed pale neutral panels, top tabs, bright green selected state, monospaced generated output and dense labelled parameter controls. The screenshot's banner/version dates it; it does not establish a current color specification.

**Assessment.** Strength: document, graph, and query tasks are separate and legible. Weakness for Docket: low-level retrieval vocabulary and always-visible token controls ask ordinary users to understand internals. Borrow separation and property inspection; default to named evidence and hide technical tuning in Settings. Do not copy the bright graph-tool aesthetic.

## 7. OpenSPG KAG

**Documented/observed inventory.** The newer guide screenshots show top-level Knowledge Base and Application; model configuration lists added/available providers, and the account menu contains Model Configuration, Data Source Configuration, Monitoring, User Configuration and Log out. Application List opens a New Application dialog. An older exploration screenshot instead shows Home/KAG, Knowledge Base Build, Reasoning Q&A, Configuration, with local Knowledge Base Build, Knowledge Model and Knowledge Exploration. These are two navigation generations, not one combined menu. Releases additionally establish upload, extraction preview and Q&A. [Private knowledge guide](https://openspg.yuque.com/ndx6g9/docs_en/dao61dh2gv1ru6zy), [exploration](https://openspg.yuque.com/ndx6g9/docs_en/ukrnmmmt5f6guz27), [applications](https://openspg.yuque.com/ndx6g9/docs_en/rh5hmy19anyb0pga), [releases](https://github.com/OpenSPG/openspg/releases).

**Primary workflows.** Configure models, build a knowledge base, inspect extracted structure, associate knowledge with an application, ask in Simple/Deep Reasoning modes, return to references. The mode/reference capabilities are release-documented; the screenshots establish management/exploration surfaces, not an executed end-to-end session. [KAG overview](https://github.com/OpenSPG/KAG).

**Advanced organization.** Modeling, knowledge construction and reasoning are distinct capabilities; application composition is separated from the knowledge base in 0.8. The observed exploration UI filters a knowledge table by type/name and offers Canvas Profiler, while model/provider management is outside that work view. [0.8 release](https://github.com/OpenSPG/openspg/releases/tag/v0.8), [exploration guide](https://openspg.yuque.com/ndx6g9/docs_en/ukrnmmmt5f6guz27).

**Context panels/progressive disclosure.** Observed account dropdown reveals global configuration; New Application is a focused modal with information, alias and description fields. A persistent evidence inspector or graph-selection gesture is not established by these images. Do not equate kg-builder/kg-solver modules with literal navigation destinations. [Application guide](https://openspg.yuque.com/ndx6g9/docs_en/rh5hmy19anyb0pga).

**Keyboard.** No reliable official product shortcut inventory found in the reviewed material. **Visual hierarchy:** observed light blue/white administrative canvas, blue primary actions, wide search/filter forms and structured tables; an older screenshot has a local sidebar. Provider cards emphasize configuration rather than evidence reading. No exact typography/spacing or keyboard-focus behavior was measured. The separate supply-chain animation is conceptual, not a UI screenshot.

**Assessment.** Strength: separates construction, exploration and application; tabular exploration offers a practical alternative to a graph picture. Potential weakness for a personal workspace: schema/index vocabulary and the visible provider/configuration surface introduce setup complexity. This is a learnability inference, not a measured defect. Borrow provenance and deliberate deeper reasoning; keep Docket's entity table understandable without graph-engine knowledge. Full current guide text and undocumented shortcuts remain unverified.

## 8. OpenJarvis

**Source-confirmed inventory.** Sidebar/routes contain Chat, Dashboard, Data Sources, Agents, Logs, Settings, Get Started; conversations and model/search controls coexist with destinations. Setup and sharing/analytics opt-in are contextual. Documentation establishes a desktop window around the chat frontend plus energy/telemetry views and tray integration. [Pinned sidebar](https://github.com/open-jarvis/OpenJarvis/blob/6656b7fed28e2ff1efa4e77987cf351f2d71eec0/frontend/src/components/Sidebar/Sidebar.tsx), [app routes](https://github.com/open-jarvis/OpenJarvis/blob/6656b7fed28e2ff1efa4e77987cf351f2d71eec0/frontend/src/App.tsx), [downloads](https://open-jarvis.github.io/OpenJarvis/downloads/).

**Primary workflows.** Set up a local backend, select a model, chat, configure an agent/data source, inspect runtime metrics/logs. Presets also provide deeper research and scheduled work, but CLI preset names are not treated as additional screens. [Quickstart](https://github.com/open-jarvis/OpenJarvis), [downloads](https://open-jarvis.github.io/OpenJarvis/downloads/).

**Advanced organization/disclosure.** Dedicated operational/configuration pages keep runtime material separate from chat. A component named CommandPalette is currently model-oriented: its installed/discover/cloud model surfaces should not be misreported as universal destination search. [Palette source](https://github.com/open-jarvis/OpenJarvis/blob/6656b7fed28e2ff1efa4e77987cf351f2d71eec0/frontend/src/components/CommandPalette.tsx).

**Context panels.** Chat component structure includes tool-call cards, research timeline, and system panel; source presence alone does not establish every expanded interaction. The inspected opt-in dialog distinguishes sharing choices and continuing without sharing. [Chat components](https://github.com/open-jarvis/OpenJarvis/tree/6656b7fed28e2ff1efa4e77987cf351f2d71eec0/frontend/src/components/Chat).

**Keyboard.** Palette source handles Escape, arrows, and Enter for installed-model selection. A complete application navigation shortcut map is not established here; do not claim universal command routing from the component's name.

**Visual hierarchy.** Only the opt-in screenshot was visually inspected: dark compact panel, subdued helper copy, outlined alternatives, cyan primary action. Full-shell visual claims remain source/document limited.

**Assessment.** Strength: operational transparency has named destinations. Weakness for Docket: dashboard/model controls and savings/analytics prompts can draw attention away from document evidence. That is a fit assessment, not a criticism of its research purpose. Borrow operational separation; omit savings/leaderboard concepts and do not adopt its runtime or theme.

## 9. Screenpipe

**Documented/source-confirmed inventory.** Reviewed development screenshot: Chat, Meetings, Timeline, Activity, Library, Scheduled, Connections, Settings, plus recents and chat tabs. Older docs call automation surfaces Pipes, whereas newer docs describe scheduled tasks; these are versioned terms, not invented parallel destinations. [Official working-set image](https://raw.githubusercontent.com/screenpipe/screenpipe/main/.github/pr-assets/chat-tabs-sidebar/01-working-set.png), [current documentation index](https://docs.screenpipe.com/llms.txt), [sidebar source](https://github.com/screenpipe/screenpipe/blob/main/apps/screenpipe-app-tauri/components/sidebar-nav-list.tsx).

**Primary workflows.** Search captured history by text/app/time, inspect timeline, select context to ask about it, manage scheduled tasks and connections. Precise lookup and synthesis are described as different routes. [Search history](https://docs.screenpi.pe/search-screen-history), [scheduled tasks](https://docs.screenpipe.com/scheduled-tasks.md).

**Advanced organization/disclosure.** Recording controls, connections, task configuration and privacy have dedicated flows. Sidebar customization reveals hide/show operations through visible menus as well as context menus. Chat mentions expose source/time constraints. [Recording controls](https://docs.screenpipe.com/recording-controls.md), [sidebar source](https://github.com/screenpipe/screenpipe/blob/main/apps/screenpipe-app-tauri/components/sidebar-nav-list.tsx), [search](https://docs.screenpi.pe/search-screen-history).

**Context panels.** Tab working set is distinct from persistent recents. Search/time selection precedes synthesis, and context pills can be removed. Do not copy continuous capture into Docket's source model.

**Keyboard.** Inspected keyboard-map artifact shows command menu Cmd+K, sidebar Cmd+B, new chat Cmd+N, tab/recents switching and separate configurable global shortcuts. Platform/release defaults must be checked before reuse. [Shortcut image](https://raw.githubusercontent.com/screenpipe/screenpipe/main/.github/pr-assets/chat-tabs-sidebar/06-shortcut-guide.png).

**Visual hierarchy.** Observed dark grey nav, darker content, compact labels/recents, thin selection rule, top working tabs and bottom composer. A screenshot's bright focus outline is not taken as brand accent.

**Assessment.** Strength: time/source context makes a retrieval boundary tangible. Weakness for Docket: capture, memory, workflows, and automation require explicit distinctions so users know what was observed and what actually ran. The official docs themselves explain those distinctions. [Recording vs automation](https://docs.screenpipe.com/recording-memory-and-automation.md). Borrow context boundaries and honest run receipts, not always-on recording.

## 10. Raycast

**Documented inventory.** Root Search/launcher, result/extension list/grid/detail/form views, contextual Action Panel, settings, extension management, Quick AI and AI Chat. These are command/view types, not a six-item application sidebar. [Quickstart](https://manual.raycast.com/quickstart), [UI reference](https://developers.raycast.com/api-reference/user-interface/action-panel).

**Primary workflows.** Invoke → type a target → select → execute default action; open an Action Panel for alternatives; configure aliases/hotkeys for repetition. Quick AI is distinct from a longer AI Chat. [Quickstart](https://manual.raycast.com/quickstart).

**Advanced organization/disclosure.** Commands are searchable; the selected object determines available actions. Grouped sections and submenus hold configuration/management. Inline shortcut recording keeps a configuration step near its command. [Action Panel](https://manual.raycast.com/action-panel).

**Context panels.** The action search is local to the current object; a selected default action and key hints reduce the need to show all operations in a toolbar. A submenu can replace the panel and Escape returns one level. [Action Panel](https://manual.raycast.com/action-panel).

**Keyboard.** Cmd/Ctrl+K opens actions; Enter activates the primary action; Cmd/Ctrl+Enter submits a form; Escape closes/goes back. The developer API also distinguishes primary/secondary defaults by view type. [Manual](https://manual.raycast.com/action-panel), [API](https://developers.raycast.com/api-reference/user-interface/action-panel).

**Visual hierarchy.** Observed object title, high-contrast selected row, icon/text alignment, sparse rules between action groups, and right-aligned keycaps. The screenshot's translucent warm background is incidental to the mechanism; Docket should not inherit translucency.

**Assessment.** Strength: commands become discoverable exactly where useful. Weakness for Docket: launcher-first navigation and immediate execution can conceal the consequence of destructive actions. This is an interaction-risk inference from default-action behavior, not an observed incident. Borrow contextual naming, search, key hints and safe defaults; keep a visible navigation hierarchy and deliberate approval forms.

## 11. Linear

**Documented inventory.** Personal Inbox/My Issues, workspace/team views, issues, projects, initiatives, documents, search/command menu and settings; enabled features and sidebar customization affect what appears. Issue lists/boards and project/initiative timelines are view arrangements, not unrelated apps. [Display options](https://linear.app/docs/display-options), [documents](https://linear.app/docs/documents), [sidebar/settings changes](https://linear.app/changelog/page/7), [assignment workflow](https://linear.app/docs/assigning-issues).

**Primary workflows.** Find/triage an issue, inspect its detail, change properties, organize work into a project, and return to the list. Inbox can expose the selected issue alongside notifications. [Triage](https://linear.app/docs/triage), [split Inbox](https://linear.app/changelog/page/15).

**Advanced organization/disclosure.** Contextual view options control grouping/order/layout. Property menus, issue peek and a command menu keep seldom-used actions off the main canvas. Settings are separated by scope. [Display options](https://linear.app/docs/display-options), [context menus](https://linear.app/changelog/2020-08-26-better-menus-and-view-options).

**Context panels.** Split list/detail enables review without losing place; text selection exposes formatting tools. Each surface shares a predictable location/view/content structure. [Editor](https://linear.app/docs/editor), [design article](https://linear.app/now/behind-the-latest-design-refresh).

**Keyboard.** Cmd/Ctrl+K is context-aware (including link editing inside the editor); G then T opens Triage, Shift+V display options, and `[` collapses/expands the sidebar. Shortcut scope matters. [Editor](https://linear.app/docs/editor), [Triage](https://linear.app/docs/triage), [display options](https://linear.app/docs/display-options), [sidebar](https://linear.app/changelog/unpublished-collapsible-sidebar).

**Visual hierarchy.** Official refresh reduces sidebar prominence and standardizes headers/view controls; the inspected anatomy graphic separates app, location, view bar and content. Borrow hierarchy rather than a pixel-identical dark theme. [Design explanation](https://linear.app/now/behind-the-latest-design-refresh).

**Assessment.** Strength: stable object hierarchy and repeated controls support dense professional work. Weakness for Docket: numerous organizational levels and context-dependent shortcuts could burden a single-user document app. This is a fit/learnability inference, not evidence of poor performance. Borrow restrained navigation, list/detail continuity and progressive disclosure; omit team/project-management structures that Docket does not need.

## Cross-product decisions for Docket

| Pattern grounded above | Adopt | Adapt / avoid | Docket consequence |
| --- | --- | --- | --- |
| Linear location/view/content hierarchy | Predictable headers, receding navigation | No task-management hierarchy or copied brand colors | Six labelled destinations; local toolbars |
| Raycast/Msty commands | Context labels, search, key hints, safe default | No global launcher or model-first palette | In-app universal command palette |
| RAGFlow/LightRAG separation | Library and Knowledge separate from answering | No exposed retrieval/token controls by default | Sources/documents/processing and entity/provenance views |
| AnythingLLM/Khoj context | Visible selected source/material | No blending library-only answers with general/cloud modes | Scope alongside every request; evidence inspector |
| Screenpipe history/time boundary | Explicit scope, task receipts, separation of live and saved work | No screen/audio capture product expansion | Activity timestamps, last-checked health, retention disclosure |
| KAG reasoning/reference model | Deliberate deeper work, tabular exploration and evidence links | No conflation of historical navigation generations, no graph-first requirement | Investigations and located relationships gated by milestones |
| OpenJarvis operations | Named resource/log surfaces | No savings dashboard, neon accents or opt-in leaderboard | Resources inside Activity; quiet runtime diagnostics |

Research supports a document-centered workspace with contextual intelligence. It does not establish that copying any competitor's agent, framework, sidebar, or color scheme improves Docket's accuracy or usability. All claimed Docket gains remain hypotheses for the exact prototype workflows in [screen specifications §19](DOCKET_SCREEN_SPECIFICATIONS.md#19-exact-workflows-to-prototype-and-test-before-implementation).
