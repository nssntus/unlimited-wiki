(function () {
  "use strict";

  var messages = {
    "zh-CN": {
      "meta.description": "Unlimited Wiki 是一个本地优先、默认私有、可控分享的 Markdown 知识平台。",
      "meta.ogDescription": "让知识在私有空间里生长，在公共广场中被看见。",
      "nav.skip": "跳到正文",
      "nav.label": "主导航",
      "nav.workflow": "工作流",
      "nav.boundaries": "安全边界",
      "nav.stack": "技术栈",
      "nav.openMenu": "打开导航",
      "nav.closeMenu": "关闭导航",
      "language.label": "语言",
      "language.switch": "English",
      "hero.statement": "让知识在私有空间里生长，\n在公共广场中被看见。",
      "hero.summary": "整理 Raw 原料，沉淀 Markdown 正本，协作治理，并通过 AI 预审与人工审核安全公开。",
      "hero.footnote": "本地优先 · 默认私有 · Markdown 可携带",
      "hero.previewLabel": "Unlimited Wiki 产品界面预览",
      "hero.next": "继续到工作流",
      "hero.nextLabel": "下一节：从原料到知识正本",
      "cta.github": "在 GitHub 查看源码",
      "cta.workflow": "探索工作流",
      "preview.search": "搜索页面…",
      "preview.knowledge": "知识库",
      "preview.guides": "01 指南",
      "preview.product": "02 产品",
      "preview.entry": "产品愿景与原则",
      "preview.engineering": "03 工程",
      "preview.operations": "04 运营",
      "preview.edit": "编辑 Markdown",
      "preview.safePreview": "安全预览",
      "preview.title": "产品愿景与原则",
      "preview.body": "Unlimited Wiki 为团队提供一个本地优先、默认私有、可控分享的知识平台。",
      "preview.principles": "核心原则",
      "preview.local": "本地优先，数据可控",
      "preview.private": "默认私有，最小公开",
      "preview.markdown": "Markdown 优先，开放可携带",
      "preview.sources": "原始资料",
      "preview.release": "发布流程",
      "preview.draft": "草稿",
      "preview.preflight": "AI 预审",
      "preview.human": "人工审核",
      "preview.publish": "公开发布",
      "workflow.title": "从原料到知识正本",
      "workflow.lead": "一条可追溯、可恢复、可持续治理的知识工作流。",
      "workflow.label": "知识工作流",
      "workflow.rawTitle": "收集 Raw",
      "workflow.rawBody": "原料进入私有收件箱，保留来源标识。",
      "workflow.markdownTitle": "沉淀 Markdown",
      "workflow.markdownBody": "整理为可编辑、可链接、可迁移的正本。",
      "workflow.governTitle": "协作治理",
      "workflow.governBody": "权限、健康检查、版本与任务保持可见。",
      "workflow.publishTitle": "预审与人工审核",
      "workflow.publishBody": "不可变快照依次经过 AI 预审和人工审核。",
      "workflow.permissions": "权限",
      "workflow.health": "健康检查",
      "workflow.history": "版本历史",
      "workflow.square": "Wiki 广场",
      "workflow.squareBody": "发布不可变公开版本，保留来源、版本历史与纠错入口。",
      "workflow.publicRevision": "公开版本",
      "workflow.publicFacts": "来源 · 历史 · 纠错",
      "workflow.note": "失败可见，任务可重试，审核流程不会静默改写私有正本。",
      "boundary.title": "边界清楚，协作才安心",
      "boundary.lead": "私有正本、投稿快照与公开版本分开管理。",
      "boundary.privateTitle": "私有 Workspace",
      "boundary.markdownFiles": "Markdown 文件",
      "boundary.rawSources": "Raw 原料",
      "boundary.roles": "Owner / Editor / Viewer 权限",
      "boundary.privateRule": "默认私有",
      "boundary.reviewTitle": "投稿与审核",
      "boundary.snapshot": "精确、不可变的投稿快照",
      "boundary.ai": "统一规则的 AI 预审",
      "boundary.human": "管理员人工审核",
      "boundary.reviewRule": "只披露确认内容",
      "boundary.publicTitle": "Wiki 广场",
      "boundary.publicRevision": "公开版本与历史",
      "boundary.publicSources": "公开来源",
      "boundary.corrections": "纠错、订阅与治理",
      "boundary.publicRule": "公开版本不反向改写私有正本",
      "boundary.tenant": "强租户隔离",
      "boundary.credentials": "模型凭据加密保存",
      "boundary.retry": "任务可重试与恢复",
      "boundary.atomic": "跨文件原子写入与条件回滚",
      "boundary.deployment": "面向本机或同机 HTTPS 反向代理的单节点部署；不是托管 SaaS，也不支持多节点集群。",
      "stack.title": "Markdown 是正本，工具链保持简单",
      "stack.lead": "自托管、可审计、可迁移。",
      "stack.architecture": "开放架构",
      "stack.browser": "浏览器",
      "stack.service": "同源 Python 服务",
      "stack.workspace": "每个 Workspace 独立",
      "stack.providers": "可选模型服务",
      "quickstart.title": "快速开始",
      "quickstart.copy": "复制命令",
      "quickstart.copied": "已复制",
      "quickstart.failed": "复制失败，请手动选择命令",
      "quickstart.address": "默认访问 http://127.0.0.1:8765",
      "closing.title": "把分散资料变成自己的知识系统。",
      "closing.body": "从本地 Markdown 开始，在清晰边界内协作与分享。",
      "closing.github": "在 GitHub 探索 Unlimited Wiki",
      "closing.readme": "阅读 README"
    },
    en: {
      "meta.description": "Unlimited Wiki is a local-first Markdown knowledge platform that stays private by default and shares through explicit review.",
      "meta.ogDescription": "Grow knowledge privately. Share it with confidence.",
      "nav.skip": "Skip to content",
      "nav.label": "Primary navigation",
      "nav.workflow": "Workflow",
      "nav.boundaries": "Boundaries",
      "nav.stack": "Stack",
      "nav.openMenu": "Open navigation",
      "nav.closeMenu": "Close navigation",
      "language.label": "Language",
      "language.switch": "中文",
      "hero.statement": "Grow knowledge privately.\nShare it with confidence.",
      "hero.summary": "Turn raw sources into governed Markdown, then publish reviewed snapshots through AI preflight and human review.",
      "hero.footnote": "Local-first · Private by default · Portable Markdown",
      "hero.previewLabel": "Unlimited Wiki product interface preview",
      "hero.next": "Continue to the workflow",
      "hero.nextLabel": "Next: from raw sources to living knowledge",
      "cta.github": "View source on GitHub",
      "cta.workflow": "Explore the workflow",
      "preview.search": "Search pages…",
      "preview.knowledge": "Knowledge",
      "preview.guides": "01 Guides",
      "preview.product": "02 Product",
      "preview.entry": "Product vision & principles",
      "preview.engineering": "03 Engineering",
      "preview.operations": "04 Operations",
      "preview.edit": "Edit Markdown",
      "preview.safePreview": "Safe preview",
      "preview.title": "Product vision & principles",
      "preview.body": "Unlimited Wiki gives teams a local-first knowledge platform that is private by default and shared deliberately.",
      "preview.principles": "Core principles",
      "preview.local": "Local-first, controlled data",
      "preview.private": "Private by default, minimal disclosure",
      "preview.markdown": "Markdown-first and portable",
      "preview.sources": "Private sources",
      "preview.release": "Release workflow",
      "preview.draft": "Draft",
      "preview.preflight": "AI preflight",
      "preview.human": "Human review",
      "preview.publish": "Publish",
      "workflow.title": "From raw sources to living knowledge",
      "workflow.lead": "A traceable, recoverable workflow for knowledge that keeps improving.",
      "workflow.label": "Knowledge workflow",
      "workflow.rawTitle": "Collect Raw",
      "workflow.rawBody": "Sources enter a private inbox with provenance intact.",
      "workflow.markdownTitle": "Build Markdown",
      "workflow.markdownBody": "Shape editable, linkable, and portable canonical pages.",
      "workflow.governTitle": "Govern together",
      "workflow.governBody": "Permissions, checks, versions, and tasks stay visible.",
      "workflow.publishTitle": "AI & human review",
      "workflow.publishBody": "Immutable snapshots pass AI preflight and human review.",
      "workflow.permissions": "Permissions",
      "workflow.health": "Health checks",
      "workflow.history": "Version history",
      "workflow.square": "Public square",
      "workflow.squareBody": "Publish immutable revisions with sources, history, and correction paths.",
      "workflow.publicRevision": "Public revision",
      "workflow.publicFacts": "Sources · history · corrections",
      "workflow.note": "Failures stay visible, tasks can retry, and review never silently rewrites private sources.",
      "boundary.title": "Clear boundaries make collaboration safer",
      "boundary.lead": "Private sources, submission snapshots, and public revisions remain separate.",
      "boundary.privateTitle": "Private Workspace",
      "boundary.markdownFiles": "Markdown files",
      "boundary.rawSources": "Raw sources",
      "boundary.roles": "Owner / Editor / Viewer roles",
      "boundary.privateRule": "Private by default",
      "boundary.reviewTitle": "Submission & review",
      "boundary.snapshot": "Exact immutable submission snapshot",
      "boundary.ai": "AI preflight under shared rules",
      "boundary.human": "Human Admin review",
      "boundary.reviewRule": "Only confirmed content is disclosed",
      "boundary.publicTitle": "Public square",
      "boundary.publicRevision": "Published revisions and history",
      "boundary.publicSources": "Public sources",
      "boundary.corrections": "Corrections, subscriptions, governance",
      "boundary.publicRule": "Public revisions never rewrite private sources",
      "boundary.tenant": "Strong tenant isolation",
      "boundary.credentials": "Encrypted model credentials",
      "boundary.retry": "Retryable, recoverable jobs",
      "boundary.atomic": "Atomic multi-file writes with conditional rollback",
      "boundary.deployment": "Built for local or same-host HTTPS reverse-proxy single-node deployments; not a hosted SaaS or multi-node cluster.",
      "stack.title": "Markdown stays canonical. The stack stays understandable.",
      "stack.lead": "Self-hosted, auditable, and portable.",
      "stack.architecture": "Open architecture",
      "stack.browser": "Browser",
      "stack.service": "Same-origin Python service",
      "stack.workspace": "Isolated per Workspace",
      "stack.providers": "Optional model providers",
      "quickstart.title": "Quick start",
      "quickstart.copy": "Copy commands",
      "quickstart.copied": "Copied",
      "quickstart.failed": "Copy failed. Select the commands manually.",
      "quickstart.address": "Opens at http://127.0.0.1:8765 by default",
      "closing.title": "Turn scattered sources into your own knowledge system.",
      "closing.body": "Start with local Markdown. Collaborate and share within clear boundaries.",
      "closing.github": "Explore Unlimited Wiki on GitHub",
      "closing.readme": "Read the README"
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

  function copyQuickstart(button) {
    var targetId = button.getAttribute("data-copy-target");
    var target = targetId ? document.getElementById(targetId) : null;
    var status = document.querySelector(".copy-status");
    if (!target || !status) return;

    var content = target.textContent || "";
    var copyPromise;
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      copyPromise = navigator.clipboard.writeText(content);
    } else {
      copyPromise = new Promise(function (resolve, reject) {
        var textarea = document.createElement("textarea");
        textarea.value = content;
        textarea.setAttribute("readonly", "");
        textarea.className = "copy-fallback";
        document.body.appendChild(textarea);
        textarea.select();
        try {
          if (document.execCommand("copy")) resolve();
          else reject(new Error("copy unavailable"));
        } catch (error) {
          reject(error);
        } finally {
          textarea.remove();
        }
      });
    }

    copyPromise.then(function () {
        status.textContent = messages[activeLocale()]["quickstart.copied"];
        button.textContent = messages[activeLocale()]["quickstart.copied"];
        window.setTimeout(function () {
          status.textContent = "";
          button.textContent = messages[activeLocale()]["quickstart.copy"];
        }, 1800);
      }).catch(function () {
        status.textContent = messages[activeLocale()]["quickstart.failed"];
      });
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

    document.querySelectorAll("[data-copy-target]").forEach(function (button) {
      button.addEventListener("click", function () {
        copyQuickstart(button);
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
      if (
        event.key === "Escape" &&
        menuToggle &&
        menuToggle.getAttribute("aria-expanded") === "true"
      ) {
        setMenu(false);
        menuToggle.focus();
      }
    });
  });
})();
