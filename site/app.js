(function () {
  "use strict";

  var messages = {
    "zh-CN": {
      "meta.title": "Unlimited Wiki",
      "meta.description": "Unlimited Wiki 把 Raw 原料整理成可治理的 Markdown 正本，再通过 AI 预审与人工审核安全公开。",
      "meta.ogDescription": "让知识在私有空间里生长，在公共广场中被看见。",
      "nav.skip": "跳到正文",
      "nav.label": "主导航",
      "nav.capabilities": "能力",
      "nav.boundaries": "公开边界",
      "nav.principles": "原则",
      "nav.start": "快速开始",
      "nav.openMenu": "打开导航",
      "nav.closeMenu": "关闭导航",
      "language.label": "语言",
      "language.switch": "English",
      "hero.kicker": "本地优先 · 默认私有 · 审核后分享",
      "hero.statement": "让知识在私有空间里生长，在公共广场中被看见。",
      "hero.summary": "Unlimited Wiki 把 Raw 原料整理成可治理的 Markdown 正本，再通过明确的 AI 预审与人工审核安全公开。",
      "hero.previewLabel": "Markdown 正本与发布工作流",
      "cta.github": "在 GitHub 查看源码",
      "cta.quickstart": "阅读快速开始",
      "preview.canonical": "Markdown 正本",
      "preview.title": "决策记录：数据保留策略",
      "preview.context": "背景：统一不同来源的原料与可治理正本。",
      "preview.decision": "结论",
      "preview.minimal": "采用最小保留集",
      "preview.private": "默认私有，按流程公开",
      "preview.impact": "影响与可恢复性",
      "workflow.label": "Raw 原料到公开发布的工作流",
      "workflow.raw": "Raw 原料",
      "workflow.markdown": "Markdown 正本",
      "workflow.ai": "AI 预审",
      "workflow.human": "人工审核",
      "workflow.public": "公开发布",
      "facts.label": "Unlimited Wiki 产品事实",
      "facts.markdownTitle": "Markdown 正本",
      "facts.markdownMeta": "可携带正本",
      "facts.markdownBody": "以 Markdown 作为可治理的正本格式，便于版本控制与追踪来源。",
      "facts.workspaceTitle": "私有 Workspace",
      "facts.workspaceMeta": "默认私有",
      "facts.workspaceBody": "Raw 与 Markdown 默认私有，只在明确授权后进入投稿流程。",
      "facts.reviewTitle": "AI 预审 + 人工审核",
      "facts.reviewMeta": "双重审核",
      "facts.reviewBody": "AI 先检查质量与合规，再由管理员决定是否公开。",
      "facts.licenseMeta": "可携带文件",
      "facts.licenseBody": "开源可审计，文件可迁移，不被展示层锁定。",
      "capabilities.title": "能力",
      "capabilities.meta": "能力范围",
      "capabilities.intro": "从原料整理到受控公开，每一步都保留清晰的业务事实与恢复路径。",
      "capabilities.readTitle": "阅读与生成",
      "capabilities.readMeta": "正本沉淀",
      "capabilities.readBody": "阅读已有内容，基于你的原料与正本生成新草稿，沉淀为可治理的 Markdown。",
      "capabilities.governTitle": "治理与协作",
      "capabilities.governMeta": "协同治理",
      "capabilities.governBody": "在私有 Workspace 中组织结构、分配权限、协同编辑，确保质量与一致性。",
      "capabilities.publishTitle": "投稿与广场",
      "capabilities.publishMeta": "审核发布",
      "capabilities.publishBody": "通过投稿流程进入 AI 预审与人工审核，合格内容发布为可追溯的公开版本。",
      "boundaries.title": "公开边界",
      "boundaries.meta": "信息披露",
      "boundaries.intro": "私有 Workspace、投稿快照和 Wiki 广场分层保存；审核是一道显式边界，不是一条隐形同步通道。",
      "boundaries.tableLabel": "公开边界表格",
      "boundaries.dimension": "边界",
      "boundaries.privateTitle": "私有 Workspace",
      "boundaries.privateMeta": "私有层",
      "boundaries.snapshotTitle": "投稿快照",
      "boundaries.snapshotMeta": "审核层",
      "boundaries.publicTitle": "Wiki 广场",
      "boundaries.publicMeta": "公开层",
      "boundaries.scope": "内容范围",
      "boundaries.scopePrivate": "Raw 原料与 Markdown 正本始终私有。",
      "boundaries.scopeSnapshot": "只提交冻结快照，不回写私有空间。",
      "boundaries.scopePublic": "只展示经过审核的公开修订。",
      "boundaries.review": "AI 与审核",
      "boundaries.reviewPrivate": "私有空间可使用 AI 辅助生成与校对。",
      "boundaries.reviewSnapshot": "AI 预审只做质量与合规检查，不等于发布。",
      "boundaries.reviewPublic": "是否公开最终由管理员人工审核决定。",
      "boundaries.history": "版本与可追溯",
      "boundaries.historyPrivate": "编辑历史与草稿保留在 Workspace。",
      "boundaries.historySnapshot": "记录来源与提交时快照，便于追溯。",
      "boundaries.historyPublic": "公开修订不会反向改写任何私有源。",
      "boundaries.deployment": "面向本机或同机 HTTPS 反向代理的单节点部署；不提供托管 SaaS，也不支持多节点集群或共享网络文件系统。",
      "principles.title": "原则",
      "principles.meta": "设计原则",
      "principles.intro": "这些原则落实为权限、事务、审计、审核与恢复机制。",
      "principles.privateTitle": "默认私有",
      "principles.privateBody": "所有内容默认私有，公开需要显式流程与授权。",
      "principles.controlTitle": "用户控制",
      "principles.controlBody": "你决定谁能访问、编辑与发布，系统不越权。",
      "principles.tenantTitle": "强租户隔离",
      "principles.tenantBody": "Workspace 与数据严格隔离，权限最小化。",
      "principles.sourceTitle": "来源可追溯",
      "principles.sourceBody": "每一份公开内容都可追溯到其私有来源与快照。",
      "principles.reviewTitle": "AI 预审 + 人工发布",
      "principles.reviewBody": "AI 用于预审，人工负责最终发布决策。",
      "principles.failureTitle": "失败可见且可恢复",
      "principles.failureBody": "流程可观测，失败可见，并支持重试与回滚。",
      "closing.title": "从本地 Markdown 开始。",
      "closing.subtitle": "可携带的 Markdown 正本。",
      "closing.github": "在 GitHub 查看源码",
      "closing.readme": "阅读 README",
      "closing.note": "此页面是静态介绍页。Unlimited Wiki 应用继续在本机或同机 HTTPS 反向代理环境中以单节点运行。",
      "notFound.metaTitle": "页面未找到 · Unlimited Wiki",
      "notFound.label": "404",
      "notFound.title": "这里没有这个页面。",
      "notFound.body": "链接可能已经移动，返回首页继续了解 Unlimited Wiki。",
      "notFound.return": "返回 Unlimited Wiki"
    },
    en: {
      "meta.title": "Unlimited Wiki",
      "meta.description": "Unlimited Wiki turns raw material into governed Markdown, then makes sharing explicit through AI preflight and human review.",
      "meta.ogDescription": "Let knowledge grow in private, then make the right parts visible.",
      "nav.skip": "Skip to content",
      "nav.label": "Primary navigation",
      "nav.capabilities": "Capabilities",
      "nav.boundaries": "Boundaries",
      "nav.principles": "Principles",
      "nav.start": "Start",
      "nav.openMenu": "Open navigation",
      "nav.closeMenu": "Close navigation",
      "language.label": "Language",
      "language.switch": "中文",
      "hero.kicker": "LOCAL-FIRST · PRIVATE BY DEFAULT · REVIEWED SHARING",
      "hero.statement": "Let knowledge grow in private, then make the right parts visible.",
      "hero.summary": "Unlimited Wiki turns raw material into governed Markdown, then makes sharing explicit through AI preflight and human review.",
      "hero.previewLabel": "Canonical Markdown and publishing workflow",
      "cta.github": "View source on GitHub",
      "cta.quickstart": "Read the quick start",
      "preview.canonical": "Canonical Markdown",
      "preview.title": "Decision record: data retention",
      "preview.context": "Context: unify raw sources and governed canonical files.",
      "preview.decision": "Decision",
      "preview.minimal": "Keep the smallest useful set",
      "preview.private": "Private by default, shared by process",
      "preview.impact": "Impact and recovery",
      "workflow.label": "Workflow from raw material to public release",
      "workflow.raw": "Raw material",
      "workflow.markdown": "Canonical Markdown",
      "workflow.ai": "AI preflight",
      "workflow.human": "Human review",
      "workflow.public": "Public release",
      "facts.label": "Unlimited Wiki product facts",
      "facts.markdownTitle": "Canonical Markdown",
      "facts.markdownMeta": "Portable source of truth",
      "facts.markdownBody": "Markdown remains the governed source of truth, ready for versioning and source tracing.",
      "facts.workspaceTitle": "Private workspaces",
      "facts.workspaceMeta": "Private by default",
      "facts.workspaceBody": "Raw sources and Markdown stay private until an explicit submission begins.",
      "facts.reviewTitle": "AI preflight + human review",
      "facts.reviewMeta": "Two-stage review",
      "facts.reviewBody": "AI checks quality and policy; an administrator decides what becomes public.",
      "facts.licenseMeta": "Portable files",
      "facts.licenseBody": "Open source, auditable, and built around portable files.",
      "capabilities.title": "Capabilities",
      "capabilities.meta": "Product scope",
      "capabilities.intro": "From source intake to controlled publishing, each step keeps its facts and recovery path visible.",
      "capabilities.readTitle": "Read & build",
      "capabilities.readMeta": "Governed source",
      "capabilities.readBody": "Read existing knowledge, draft from your own sources and canonical files, then keep the result as governed Markdown.",
      "capabilities.governTitle": "Govern together",
      "capabilities.governMeta": "Shared governance",
      "capabilities.governBody": "Organize structure, assign roles, and edit together inside a private Workspace with visible quality controls.",
      "capabilities.publishTitle": "Submit & publish",
      "capabilities.publishMeta": "Reviewed release",
      "capabilities.publishBody": "Move a submission through AI preflight and human review, then publish an attributable public revision.",
      "boundaries.title": "Public boundaries",
      "boundaries.meta": "Information disclosure",
      "boundaries.intro": "Private workspaces, submission snapshots, and the public square are stored separately. Review is an explicit boundary, not a hidden sync channel.",
      "boundaries.tableLabel": "Public boundary comparison",
      "boundaries.dimension": "Boundary",
      "boundaries.privateTitle": "Private Workspace",
      "boundaries.privateMeta": "Private layer",
      "boundaries.snapshotTitle": "Submission snapshot",
      "boundaries.snapshotMeta": "Review layer",
      "boundaries.publicTitle": "Public square",
      "boundaries.publicMeta": "Public layer",
      "boundaries.scope": "Content scope",
      "boundaries.scopePrivate": "Raw sources and canonical Markdown remain private.",
      "boundaries.scopeSnapshot": "Only a frozen snapshot is submitted; it never writes back.",
      "boundaries.scopePublic": "Only reviewed public revisions are displayed.",
      "boundaries.review": "AI and review",
      "boundaries.reviewPrivate": "AI may assist generation and checking inside the private space.",
      "boundaries.reviewSnapshot": "AI preflight checks quality and policy. It is not publication.",
      "boundaries.reviewPublic": "A human administrator makes the final publishing decision.",
      "boundaries.history": "Versioning and traceability",
      "boundaries.historyPrivate": "Drafts and edit history stay in the Workspace.",
      "boundaries.historySnapshot": "The snapshot records its source and submission state.",
      "boundaries.historyPublic": "Public revisions never write back to private sources.",
      "boundaries.deployment": "Designed for a local or same-host HTTPS reverse-proxy single-node deployment; not a hosted SaaS, multi-node cluster, or shared network filesystem.",
      "principles.title": "Principles",
      "principles.meta": "Design principles",
      "principles.intro": "These principles are enforced through permissions, transactions, audit, review, and recovery mechanisms.",
      "principles.privateTitle": "Private by default",
      "principles.privateBody": "Everything starts private. Publishing requires an explicit process and authorization.",
      "principles.controlTitle": "User control",
      "principles.controlBody": "You decide who can read, edit, and publish. The system does not overreach.",
      "principles.tenantTitle": "Strong tenant isolation",
      "principles.tenantBody": "Workspaces and data remain isolated under least-privilege access.",
      "principles.sourceTitle": "Source traceability",
      "principles.sourceBody": "Every public item can be traced to its private source and frozen snapshot.",
      "principles.reviewTitle": "AI preflight + human publish",
      "principles.reviewBody": "AI supports preflight; a person remains responsible for publication.",
      "principles.failureTitle": "Visible, recoverable failure",
      "principles.failureBody": "Work stays observable, failures stay visible, and retry or rollback remains possible.",
      "closing.title": "Start with local Markdown.",
      "closing.subtitle": "Start with portable Markdown.",
      "closing.github": "View source on GitHub",
      "closing.readme": "Read the README",
      "closing.note": "This is a static introduction page. The Unlimited Wiki application continues to run as a single node on a local machine or behind a same-host HTTPS reverse proxy.",
      "notFound.metaTitle": "Page not found · Unlimited Wiki",
      "notFound.label": "404",
      "notFound.title": "This page is not here.",
      "notFound.body": "The link may have moved. Return home to continue exploring Unlimited Wiki.",
      "notFound.return": "Return to Unlimited Wiki"
    }
  };

  function activeLocale() {
    return document.documentElement.dataset.locale === "en" ? "en" : "zh-CN";
  }

  function translate(locale) {
    var dictionary = messages[locale];
    document.documentElement.lang = locale;
    document.documentElement.dataset.locale = locale;

    document.querySelectorAll("[data-i18n]").forEach(function (element) {
      var key = element.getAttribute("data-i18n");
      if (key && Object.prototype.hasOwnProperty.call(dictionary, key)) {
        element.textContent = dictionary[key];
      }
    });

    document.querySelectorAll("[data-i18n-aria]").forEach(function (element) {
      var key = element.getAttribute("data-i18n-aria");
      if (key && Object.prototype.hasOwnProperty.call(dictionary, key)) {
        element.setAttribute("aria-label", dictionary[key]);
      }
    });

    document.querySelectorAll("[data-i18n-content]").forEach(function (element) {
      var key = element.getAttribute("data-i18n-content");
      if (key && Object.prototype.hasOwnProperty.call(dictionary, key)) {
        element.setAttribute("content", dictionary[key]);
      }
    });

    document.querySelectorAll("[data-locale-choice]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-locale-choice") === locale));
    });

    document.querySelectorAll("[data-locale-meta]").forEach(function (element) {
      element.setAttribute("content", locale === "en" ? "en_US" : "zh_CN");
    });

    try {
      localStorage.setItem("unlimited-wiki-locale", locale);
    } catch (_error) {
      // The selected language still applies for the current page.
    }
  }

  function setMenu(open) {
    var header = document.querySelector(".site-header");
    var toggle = document.querySelector(".menu-toggle");
    if (!header || !toggle) return;
    header.classList.toggle("menu-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("data-i18n-aria", open ? "nav.closeMenu" : "nav.openMenu");
    toggle.setAttribute("aria-label", messages[activeLocale()][open ? "nav.closeMenu" : "nav.openMenu"]);
    var symbol = toggle.querySelector("span");
    if (symbol) symbol.textContent = open ? "×" : "☰";
  }

  document.addEventListener("DOMContentLoaded", function () {
    translate(activeLocale());

    document.querySelectorAll("[data-locale-choice]").forEach(function (button) {
      button.addEventListener("click", function () {
        var locale = button.getAttribute("data-locale-choice");
        if (locale === "en" || locale === "zh-CN") translate(locale);
      });
    });

    document.querySelectorAll("[data-cycle-locale]").forEach(function (button) {
      button.addEventListener("click", function () {
        translate(activeLocale() === "en" ? "zh-CN" : "en");
      });
    });

    var menuToggle = document.querySelector(".menu-toggle");
    if (menuToggle) {
      menuToggle.addEventListener("click", function () {
        setMenu(menuToggle.getAttribute("aria-expanded") !== "true");
      });
    }

    document.querySelectorAll(".primary-nav a").forEach(function (link) {
      link.addEventListener("click", function () {
        setMenu(false);
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menuToggle && menuToggle.getAttribute("aria-expanded") === "true") {
        setMenu(false);
        menuToggle.focus();
      }
    });
  });
})();
