/**
 * Build offline provider comparisons with the actual Workspace renderer.
 * Run: node workspace/tests/fixtures/render_zulip_messages_preview.mjs [--telegram]
 * Requires sibling zulip/workspace_ui checkouts and workspace_ui/node_modules.
 * Extra Zulip dependencies are installed in the system temporary directory.
 */
import * as fs from "node:fs/promises";
import * as module from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import * as childProcess from "node:child_process";
import * as url from "node:url";
import * as assert from "node:assert/strict";

const telegram = process.argv.includes("--telegram");
const providerName = telegram ? "Telegram" : "Zulip";
const fixtureName = telegram ? "telegram_messages" : "zulip_messages";

const directory = path.dirname(url.fileURLToPath(import.meta.url));
const repository = path.resolve(directory, "../../..");
const zulip =
  process.env.ZULIP_SOURCE_DIR ?? path.resolve(repository, "../zulip");
const workspace =
  process.env.WORKSPACE_UI_SOURCE_DIR ??
  path.resolve(repository, "../workspace_ui");
const requireUi = module.createRequire(path.join(workspace, "package.json"));
const dependencies = path.join(os.tmpdir(), "workspace-zulip-preview-deps");
const requireExtra = module.createRequire(
  path.join(dependencies, "package.json"),
);
const extraPackages = [
  "date-fns@4.1.0",
  "katex@0.16.28",
  "minimalistic-assert@1.0.1",
];
if (
  extraPackages.some((item) => {
    try {
      requireExtra.resolve(item.split("@")[0]);
      return false;
    } catch {
      return true;
    }
  })
) {
  childProcess.execFileSync(
    "npm",
    [
      "install",
      "--prefix",
      dependencies,
      "--no-save",
      "--package-lock=false",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      ...extraPackages,
    ],
    { stdio: "inherit" },
  );
}
const esbuild = requireUi("esbuild");
const handlebars = requireUi("handlebars");
const jsdom = requireUi("jsdom");
const postcss = requireUi("postcss");
const tailwindcss = requireUi("@tailwindcss/postcss");
const svgr = requireUi("@svgr/core");
const temporary = await fs.mkdtemp(
  path.join(os.tmpdir(), "workspace-message-render-"),
);
const dom = new jsdom.JSDOM("<!doctype html><html><body></body></html>");
for (const key of [
  "window",
  "document",
  "Element",
  "Node",
  "HTMLElement",
  "HTMLAnchorElement",
]) {
  globalThis[key] = dom.window[key];
}

const preserveLineEndSpaces = (value) =>
  value.replace(/[ \t]+$/gm, (spaces) =>
    spaces.replaceAll(" ", "&#32;").replaceAll("\t", "&#9;"),
  );
const escape = (value) =>
  preserveLineEndSpaces(
    String(value).replace(
      /[&<>"']/g,
      (character) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[character],
    ),
  );
const asset = (label, video = false) =>
  "data:image/svg+xml;base64," +
  Buffer.from(
    `<svg xmlns="http://www.w3.org/2000/svg" width="480" height="240" viewBox="0 0 480 240">
  <rect width="480" height="240" rx="12" fill="#eef3f8"/>
  <path d="M0 175L125 90L230 170L350 65L480 155V240H0Z" fill="#d7e3ef"/>
  <circle cx="392" cy="51" r="19" fill="#a9c2da"/>
  ${video ? '<circle cx="240" cy="94" r="28" fill="#375a78"/><path d="M233 80L254 94L233 108Z" fill="white"/>' : ""}
  <rect x="18" y="182" width="444" height="42" rx="7" fill="white" fill-opacity=".94"/>
  <text x="33" y="208" fill="#344e68" font-family="sans-serif" font-size="15">${escape(label)}</text>
  </svg>`,
  ).toString("base64");

// Synthetic Telegram entity reference view, independent of Workspace Markdown.
function telegramBody(message) {
  const text = message.content;
  const entities = [...message.entities].sort((a, b) => a.offset - b.offset || b.length - a.length);
  function render(start, end, values) {
    let output = "", cursor = start;
    while (values.length) {
      const [entity, ...rest] = values;
      const right = entity.offset + entity.length;
      output += escape(text.slice(cursor, entity.offset));
      let body = render(entity.offset, right, rest.filter(item => item.offset < right));
      const tags = {MessageEntityBold: "strong", MessageEntityItalic: "em", MessageEntityStrike: "del", MessageEntityUnderline: "u", MessageEntityCode: "code", MessageEntityPre: "pre", MessageEntityBlockquote: "blockquote"};
      if (tags[entity.type]) body = `<${tags[entity.type]}>${body}</${tags[entity.type]}>`;
      if (entity.type === "MessageEntitySpoiler") body = `<span class="telegram-spoiler" tabindex="0" title="Spoiler: focus or hover to reveal">${body}</span>`;
      if (entity.type === "MessageEntityTextUrl") {
        const href = /^(https?:|mailto:|tg:)/.test(entity.url) ? escape(entity.url) : "#";
        body = `<a href="${href}">${body}</a>`;
      }
      output += body; cursor = right; values = rest.filter(item => item.offset >= right);
    }
    return output + escape(text.slice(cursor, end));
  }
  return `<div style="white-space:pre-wrap">${render(0, text.length, entities)}</div>`;
}

function helpers() {
  const names = new Map([
    [6, "Notification Bot"],
    [9, "Alice Example"],
    [12, "Bob Example"],
    [13, "Zoë Example"],
    [14, "Alex Example"],
    [15, "Alex Example"],
  ]);
  const emoji = new Map([
    ["smile", "1f604"],
    ["+1", "1f44d"],
    ["heart", "2764"],
  ]);
  const linkifier = /TASK-(\d+)\b/g;
  const linkifiers = new Map([
    [
      linkifier,
      {
        group_number_to_name: { 1: "id" },
        url_template: {
          expand: ({ id }) => `https://issues.example.test/TASK-${id}`,
        },
      },
    ],
  ]);
  return {
    get_actual_name_from_user_id: (id) => names.get(id),
    get_user_id_from_name: (name) =>
      [...names].find(([, value]) => value === name)?.[0],
    is_valid_full_name_and_user_id: (name, id) => names.get(id) === name,
    my_user_id: () => 9,
    is_valid_user_id: (id) => names.has(id),
    get_user_group_from_name: (name) =>
      name === "Engineering" ? { id: 17, name } : undefined,
    is_member_of_user_group: () => true,
    get_stream_by_name: (name) =>
      name === "general" ? { stream_id: 42, name } : undefined,
    stream_hash: (id) => `#narrow/channel/${id}-general`,
    stream_topic_hash: (id, topic) =>
      `/#narrow/channel/${id}-general/topic/${encodeURIComponent(topic)}`,
    should_translate_emoticons: () => false,
    get_emoji_name: (code) =>
      [...emoji].find(([, value]) => value === code)?.[0],
    get_emoji_codepoint: (name) => emoji.get(name),
    get_emoticon_translations: () => [],
    get_realm_emoji_url: (name) =>
      name === "party_parrot" ? asset("Custom emoji") : undefined,
    get_linkifier_map: () => linkifiers,
  };
}

function widgetPreview(message) {
  const data = message.submessages.map((item) => JSON.parse(item.content));
  const initial = data.find((item) => item.widget_type)?.extra_data;
  if (!initial) return null;
  if (data[0].widget_type === "poll") {
    const votes = new Map();
    message.submessages.forEach((submessage, index) => {
      const event = data[index];
      if (event.type === "vote") {
        const voters = votes.get(event.key) ?? new Set();
        if (event.vote === 1) voters.add(submessage.sender_id);
        else voters.delete(submessage.sender_id);
        votes.set(event.key, voters);
      }
    });
    return `<section class="widget"><div class="widget-label">POLL · READ-ONLY SNAPSHOT</div>
      <strong>${escape(initial.question)}</strong><ul>${initial.options
        .map(
          (option, index) =>
            `<li><span class="poll-circle"></span><span>${escape(option)}</span><b>${votes.get(`canned,${index}`)?.size ?? 0}</b></li>`,
        )
        .join("")}</ul></section>`;
  }
  const completed = new Set(
    data.filter((item) => item.type === "strike").map((item) => item.key),
  );
  return `<section class="widget"><div class="widget-label">TO-DO · READ-ONLY SNAPSHOT</div>
    <strong>${escape(initial.task_list_title)}</strong><ul>${initial.tasks
      .map((task, index) => {
        const checked = completed.has(`${index},${message.sender_id}`);
        return `<li><input type="checkbox" disabled ${checked ? "checked" : ""} aria-label="${escape(task.task)}"><span class="${checked ? "completed" : ""}">${escape(task.task)}${task.desc ? `<small>${escape(task.desc)}</small>` : ""}</span></li>`;
      })
      .join("")}</ul></section>`;
}

function prepareBody(html, example, side, renderers) {
  const root = dom.window.document.createElement("div");
  root.innerHTML = html;
  root.querySelectorAll(".spoiler-header").forEach((header) => {
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");
    header.setAttribute("aria-expanded", "false");
  });
  root.querySelectorAll("span.emoji").forEach((element) => {
    const code = /(?:^|\s)emoji-([0-9a-f-]+)(?:\s|$)/.exec(
      element.className,
    )?.[1];
    if (code)
      element.textContent = String.fromCodePoint(
        ...code.split("-").map((part) => parseInt(part, 16)),
      );
  });
  root.querySelectorAll("img:not(.emoji)").forEach((image) => {
    const source = image.getAttribute("src") ?? "";
    const host = image.closest("[data-workspace-file]");
    const name =
      host?.getAttribute("data-workspace-file-name") ||
      image.alt ||
      decodeURIComponent(source.split("/").pop() || "Image");
    image.src = asset(name);
    image.alt = `${name} — synthetic preview asset`;
    image.removeAttribute("loading");
    if (host && side === "workspace") {
      image.classList.add(
        "message-media-preview",
        "workspace-message-file-preview-image",
      );
      host.classList.add("workspace-message-file-preview-loaded");
      host.dataset.workspacePreviewStatus = "loaded";
      host.querySelector(".workspace-message-file-placeholder__label").hidden =
        true;
    }
  });
  root
    .querySelectorAll(
      ".workspace-message-file-placeholder, .workspace-message-attachment-host",
    )
    .forEach((host) => {
      host.removeAttribute("style");
      const name =
        host.getAttribute("data-workspace-file-name") ||
        host.getAttribute("title") ||
        "Attachment";
      if (host.getAttribute("data-workspace-media-kind") === "video") {
        host.innerHTML = `<img class="message-media-preview workspace-message-file-preview-image" src="${asset(name, true)}" alt="${escape(name)} — synthetic video preview">`;
        host.classList.add("workspace-message-file-preview-loaded");
      }
      if (host.classList.contains("workspace-message-attachment-host")) {
        host.innerHTML = renderers.reactServer.renderToStaticMarkup(
          renderers.react.createElement(
            renderers.attachments.AttachmentReadyCard,
            {
              fileName: name,
              contentType: host.dataset.workspaceFileContentType,
              sizeBytes: host.hasAttribute("data-workspace-file-size")
                ? Number(host.dataset.workspaceFileSize)
                : undefined,
              appearance: "embedded",
              role: "presentation",
            },
          ),
        );
        host.classList.add("workspace-message-attachment-host--static");
        host.setAttribute("role", "group");
        host.removeAttribute("tabindex");
      }
    });
  if (side === "zulip") {
    root.querySelectorAll("a").forEach((link) => {
      const href = link.getAttribute("href") ?? "";
      if (
        !/\/user_uploads\//.test(href) &&
        !/^https:\/\/images\.example\.test\//.test(href)
      )
        return;
      const name = decodeURIComponent(href.split("/").pop());
      if (/\.(png|gif)$/i.test(name)) {
        if (!link.querySelector("img"))
          link.innerHTML = `<img src="${asset(name)}" alt="${escape(name)} — synthetic preview asset">`;
        link.classList.add("fixture-media");
      } else if (/\.mp4$/i.test(name)) {
        link.classList.add("fixture-media");
        link.innerHTML = `<img src="${asset(name, true)}" alt="${escape(name)} — synthetic video preview">`;
      } else link.classList.add("fixture-attachment");
    });
    if (example.message.is_me_message) {
      const paragraph = root.querySelector("p");
      paragraph.firstChild.textContent =
        paragraph.firstChild.textContent.replace(/^\/me /, "");
      const author = dom.window.document.createElement("strong");
      author.textContent = example.message.sender_full_name + " ";
      paragraph.prepend(author);
      paragraph.classList.add("action-message");
    }
  }
  root.querySelectorAll("a").forEach((link) => {
    const href = link.getAttribute("href") ?? "";
    link.title ||= href;
    if (/^https?:/.test(href)) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  });
  root.querySelectorAll("time").forEach((time) => {
    time.title = time.getAttribute("datetime");
    time.textContent =
      new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Europe/Moscow",
      }).format(new Date(time.getAttribute("datetime"))) + " MSK";
  });
  return preserveLineEndSpaces(root.innerHTML);
}

try {
  const bundle = path.join(temporary, "renderers.cjs");
  const renderDirectory = path.join(
    workspace,
    "packages/web/src/shared/lib/workspace-message-render",
  );
  await esbuild.build({
    stdin: {
      contents: `
      import * as zulip from ${JSON.stringify(path.join(zulip, "web/src/markdown.ts"))};
      import * as parse from ${JSON.stringify(path.join(renderDirectory, "workspace-message-parse.lib.ts"))};
      import * as marked from ${JSON.stringify(path.join(renderDirectory, "workspace-message-marked.lib.ts"))};
      import * as render from ${JSON.stringify(path.join(renderDirectory, "workspace-message-render.lib.ts"))};
      import * as reference from ${JSON.stringify(path.join(workspace, "packages/web/src/shared/lib/workspace-reference-urn.lib.ts"))};
      import * as fileReference from ${JSON.stringify(path.join(renderDirectory, "workspace-message-file-reference.lib.ts"))};
      import * as quote from ${JSON.stringify(path.join(workspace, "packages/web/src/shared/lib/workspace-message-quote.lib.ts"))};
      import * as quoteFrame from ${JSON.stringify(path.join(workspace, "packages/web/src/shared/ui/workspace-message-quote-frame"))};
      import * as body from ${JSON.stringify(path.join(workspace, "packages/web/src/entities/messenger/messenger-workspace-message-body.ui.tsx"))};
      import * as attachments from ${JSON.stringify(path.join(workspace, "packages/web/src/shared/ui/attachment-card"))};
      import * as theme from ${JSON.stringify(path.join(workspace, "packages/web/src/shared/lib/themes/blue-cold.ts"))};
      import * as react from "react";
      import * as reactServer from "react-dom/server";
      export {zulip, parse, marked, render, reference, fileReference, quote, quoteFrame, body, attachments, theme, react, reactServer};`,
      resolveDir: workspace,
    },
    bundle: true,
    platform: "node",
    format: "cjs",
    outfile: bundle,
    nodePaths: [
      path.join(workspace, "node_modules"),
      path.join(dependencies, "node_modules"),
    ],
    alias: { "~": path.join(workspace, "packages/web/src") },
    plugins: [
      {
        name: "preview-context",
        setup(build) {
          build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({
            path: path.resolve(
              args.resolveDir,
              args.path
                .replace(/^~\//, path.join(workspace, "packages/web/src/"))
                .replace(/\?react$/, ""),
            ),
            namespace: "svg-component",
          }));
          build.onLoad(
            { filter: /.*/, namespace: "svg-component" },
            async (args) => ({
              contents: await svgr.transform(
                await fs.readFile(args.path, "utf8"),
                { plugins: [requireUi.resolve("@svgr/plugin-jsx")] },
                { componentName: "SvgAsset" },
              ),
              loader: "jsx",
              resolveDir: workspace,
            }),
          );
          build.onResolve({ filter: /^\.\/util\.ts$/ }, (args) =>
            args.importer === path.join(zulip, "web/src/markdown.ts")
              ? { path: "topic-label", namespace: "preview" }
              : undefined,
          );
          build.onResolve({ filter: /^~\/shared\/lib\/logger$/ }, () => ({
            path: "logger",
            namespace: "preview",
          }));
          build.onLoad({ filter: /.*/, namespace: "preview" }, (args) => ({
            contents:
              args.path === "logger"
                ? "export const createLogger = () => ({debug(){}, warn:console.warn, error:console.error});"
                : "export const get_final_topic_display_name = topic => topic || 'general chat';",
          }));
          build.onLoad({ filter: /\.hbs$/ }, async (args) => ({
            contents: `
        import * as handlebars from ${JSON.stringify(requireUi.resolve("handlebars/runtime"))};
        export default handlebars.default.template(${handlebars.precompile(await fs.readFile(args.path, "utf8"))});`,
            resolveDir: workspace,
          }));
        },
      },
    ],
  });
  const renderers = requireUi(bundle);
  const examples = JSON.parse(
    await fs.readFile(path.join(directory, `${fixtureName}.json`), "utf8"),
  );
  const options = {
    enableMarkdown: true,
    enableMentions: true,
    enableQuotes: true,
    enableEmojiShortcodes: true,
    enableCodeHighlight: false,
    enableCodeCopy: false,
    enableProtectedMedia: true,
    enableAttachments: true,
    enableGallery: false,
  };
  const messagesById = new Map(
    examples.map((example) => [String(example.message.id), example]),
  );
  let urnCount = 0;
  function validateTokens(tokens, example) {
    for (const token of tokens) {
      const href = token.href ?? token.reference?.href;
      if (href?.startsWith("urn:")) {
        const reference = renderers.reference.parseWorkspaceReferenceUrn(href);
        const file = renderers.fileReference.parseWorkspaceMessageFileHref(
          href,
          token.text ?? "",
        );
        const external = renderers.reference.parseWorkspaceUrlUrn(href);
        assert.ok(
          reference || file || external,
          `${example.name}: invalid URN ${href}`,
        );
        const context = example.conversion_context;
        if (file)
          assert.ok(
            Object.values(context.files).includes(href),
            `${example.name}: unknown file`,
          );
        if (reference?.kind === "forward") {
          validateTokens(
            renderers.marked
              .createWorkspaceMarkdownLexer()
              .lexer(reference.snapshotMarkdown),
            example,
          );
        }
        if (reference) {
          const lookup = {
            user: "mention_uuids",
            stream: "stream_uuids",
            topic: "topic_uuids",
            message: "message_uuids",
            quote: "message_uuids",
            forward: "message_uuids",
          }[reference.kind];
          const uuid =
            reference.kind === "topic"
              ? reference.topicUuid
              : (reference.userUuid ??
                reference.streamUuid ??
                reference.messageUuid);
          assert.ok(
            Object.values(context[lookup]).includes(uuid),
            `${example.name}: unknown ${reference.kind}`,
          );
        }
        urnCount++;
      }
      if (token.type === "workspace_mention" && token.userUuid) {
        assert.ok(
          Object.values(example.conversion_context.mention_uuids).includes(
            token.userUuid,
          ),
        );
        urnCount++;
      }
      if (
        token.type === "link" &&
        !href.startsWith("urn:") &&
        !href.startsWith("mailto:")
      ) {
        assert.fail(`${example.name}: link is not a Workspace URN: ${href}`);
      }
      if (token.tokens) validateTokens(token.tokens, example);
      if (token.type === "code" || token.type === "codespan") {
        validateTokens(
          renderers.marked.createWorkspaceMarkdownLexer().lexer(token.text),
          example,
        );
      }
      for (const item of token.items ?? [])
        validateTokens(item.tokens, example);
      for (const cell of [
        ...(token.header ?? []),
        ...(token.rows?.flat() ?? []),
      ]) {
        validateTokens(cell.tokens, example);
      }
    }
  }
  function renderWorkspaceBody(example, renderOptions = options) {
    const document = renderers.parse.parseWorkspaceMessageBody(
      example.expected_payload.content,
    );
    if (renderOptions === options)
      validateTokens(
        renderers.marked
          .createWorkspaceMarkdownLexer()
          .lexer(example.expected_payload.content),
        example,
      );
    const rendered = renderers.render.renderWorkspaceMessageBodySegments(
      document,
      renderOptions,
    );
    const root = dom.window.document.createElement("div");
    root.innerHTML = renderers.reactServer.renderToStaticMarkup(
      renderers.react.createElement(renderers.body.WorkspaceMessageBody, {
        html: "",
        metadata: document.metadata,
        useInlineMeta: false,
      }),
    );
    root.firstElementChild.innerHTML = rendered.segments
      .map((segment) => {
        if (segment.kind === "html") return segment.html;
        const reference = segment.reference;
        const [sourceId] = Object.entries(
          example.conversion_context.message_uuids,
        ).find(([, uuid]) => uuid === reference.messageUuid);
        const original = messagesById.get(sourceId);
        const isSnapshot = reference.snapshotMarkdown != null;
        const quoteMarkdown = isSnapshot
          ? `[${reference.fallbackAuthorLabel}](${renderers.reference.buildWorkspaceForwardUrn(reference.messageUuid, reference.snapshotMarkdown)})`
          : renderers.quote.buildWorkspaceQuoteReference({
              senderName: original.message.sender_full_name,
              messageUuid: reference.messageUuid,
              selectedText: reference.selectedText,
            });
        assert.ok(
          example.expected_payload.content.includes(quoteMarkdown),
          `${example.name}: invalid quote header`,
        );
        const content =
          reference.selectedText == null
            ? renderers.react.createElement("div", {
                dangerouslySetInnerHTML: {
                  __html: renderWorkspaceBody(
                    isSnapshot
                      ? {
                          ...example,
                          expected_payload: {
                            kind: "markdown",
                            content: reference.snapshotMarkdown,
                          },
                        }
                      : original,
                    {
                      ...options,
                      enableProtectedMedia: false,
                      enableAttachments: false,
                      enableGallery: false,
                    },
                  ),
                },
              })
            : renderers.react.createElement(
                "div",
                {
                  className: "whitespace-pre-wrap break-words text-sm",
                },
                reference.selectedText,
              );
        return renderers.reactServer.renderToStaticMarkup(
          renderers.react.createElement(
            renderers.quoteFrame.WorkspaceMessageQuoteFrame,
            {
              header: isSnapshot
                ? reference.fallbackAuthorLabel
                : original.message.sender_full_name,
              headerProps: { "data-workspace-quote-open": "true" },
              "data-workspace-quote": "true",
              "data-workspace-quote-message-uuid": reference.messageUuid,
              "data-workspace-quote-status": isSnapshot ? "snapshot" : "ready",
            },
            content,
          ),
        );
      })
      .join("");
    return root.innerHTML;
  }
  let roundtripRendered;
  if (process.env.REVIEW_RENDER_OUTPUT) {
    const captures = JSON.parse(await fs.readFile(path.join(directory, "telegram_message_types_review.json"), "utf8"));
    const cases = [...captures.zulip_cases, ...captures.native_telegram_cases];
    for (const row of cases) messagesById.set(String(row.example.message.id), row.example);
    roundtripRendered = cases.map(row => {
      const example = row.example;
      const workspaceHtml = content => prepareBody(renderWorkspaceBody({
        ...example, expected_payload: {kind:"markdown", content},
      }, {...options}), example, "workspace", renderers);
      const zulipHtml = prepareBody(renderers.zulip.parse({
        raw_content: row.zulip.content, helper_config: helpers(),
      }).content, example, "zulip");
      return {
        id: (row.fixture_source === "telegram" ? "native-" : "") + example.name,
        workspace: workspaceHtml(example.expected_payload.content),
        zulip: zulipHtml,
        zulipReturned: workspaceHtml(row.zulip.returned.content),
        telegramReturned: workspaceHtml(row.telegram.roundtrip),
      };
    });
  }
  const cards = examples
    .map((example, index) => {
      if (!telegram && example.unsupported_features.length > 0) {
        const original = `urn:url:${example.conversion_context.original_url}`;
        const content = example.expected_payload.content;
        assert.ok(
          content.endsWith(`[Original message](${original})`),
          `${example.name}: missing original-message footer`,
        );
        assert.equal(
          content.split(`](${original})`).length - 1,
          1,
          `${example.name}: duplicate original-message links`,
        );
        assert.ok(
          !content.includes("[Not supported in Workspace:"),
          `${example.name}: unsupported notices must be plain text`,
        );
      }
      const source = telegram ? telegramBody(example.message) :
        widgetPreview(example.message) ??
        renderers.zulip.parse({
          raw_content: example.message.content,
          helper_config: helpers(),
        }).content;
      const destination = renderWorkspaceBody(example);
      if (example.workspace_assertion) {
        const root = dom.window.document.createElement("div");
        root.innerHTML = destination;
        const check = example.workspace_assertion;
        const node = check.selector ? root.querySelector(check.selector) : root;
        assert.ok(node, `${example.name}: missing ${check.selector}`);
        const readable = node.cloneNode(true); readable.querySelectorAll("br").forEach(br => br.replaceWith("\n"));
        assert.equal(readable.textContent.replace(/\s+/g, " ").trim(), check.text);
        if (check.href) assert.equal(node.getAttribute("href"), check.href);
        for (const selector of check.absent ?? []) assert.equal(root.querySelector(selector), null, `${example.name}: unexpected ${selector}`);
      }
      if (example.expected_quote_count != null) {
        const sourceRoot = dom.window.document.createElement("div");
        sourceRoot.innerHTML = source;
        const destinationRoot = dom.window.document.createElement("div");
        destinationRoot.innerHTML = destination;
        assert.equal(
          sourceRoot.querySelectorAll("blockquote").length,
          example.expected_quote_count,
          `${example.name}: missing Zulip quote blocks`,
        );
        assert.equal(
          destinationRoot.querySelectorAll("[data-workspace-quote]").length,
          example.expected_quote_count,
          `${example.name}: missing Workspace quote frames`,
        );
        assert.ok(
          !destinationRoot.textContent.includes("Not supported in Workspace:") &&
            !destinationRoot.textContent.includes("```quote"),
          `${example.name}: nested quotes leaked as literal text`,
        );
      }
      const kind =
        example.message.type === "stream"
          ? "Channel"
          : example.message.display_recipient.length > 2
            ? "Group DM"
            : "Direct message";
      const badges = example.unsupported_features.length
        ? `<span class="badge unsupported">Unsupported features</span>`
        : '<span class="badge supported">Supported</span>';
      const sentAt = new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/Moscow",
      }).format(new Date(example.message.timestamp * 1000));
      return `<article id="${escape(example.name)}" class="example" data-unsupported="${example.unsupported_features.length > 0}" data-search="${escape((example.name + " " + example.description + " " + example.message.content + " " + example.expected_payload.content).toLowerCase())}">
      <header class="example-heading"><div><span class="case-number">${String(index + 1).padStart(2, "0")}</span><a href="#${escape(example.name)}">${escape(example.name.replaceAll("_", " "))}</a></div><div class="badges"><span class="badge">${kind}</span>${badges}</div></header>
      <p class="description">${escape(example.description)}</p>
      <div class="comparison"><section class="column zulip"><h3><span class="brand-dot"></span>${providerName} <small>${example.direction === "outgoing" ? "Expected" : "Original"}</small></h3><div class="message"><div class="author"><span class="avatar">${escape(example.message.sender_full_name[0])}</span><strong>${escape(example.message.sender_full_name)}</strong><span>${sentAt}</span></div><div class="message-body rendered_markdown">${prepareBody(source, example, "zulip")}</div></div><details><summary>${telegram ? "Text and UTF-16 entities" : "Markdown source"}</summary><pre>${escape(telegram ? JSON.stringify({text:example.message.content, entities:example.message.entities}, null, 2) : example.message.content)}</pre></details></section>
      <section class="column workspace"><h3><span class="brand-dot"></span>Workspace <small>${example.direction === "outgoing" ? "Original" : "Expected"}</small></h3><div class="message"><div class="author"><span class="avatar">${escape(example.message.sender_full_name[0])}</span><strong>${escape(example.message.sender_full_name)}</strong><span>${sentAt}</span></div><div class="workspace-native"><div class="fixture-bubble" data-message-owner="${example.message.sender_id === 9 ? "own" : "peer"}">${prepareBody(destination, example, "workspace", renderers)}<div class="fixture-time">${sentAt}</div></div></div></div><details><summary>Workspace Markdown</summary><pre>${escape(example.expected_payload.content)}</pre></details></section></div>
      ${example.unsupported_features.length ? `<div class="feature-note">${example.unsupported_features.map(escape).join(" · ")}</div>` : ""}
      <details class="fixture-data"><summary>Fixture data</summary><pre>${escape(JSON.stringify(example, null, 2))}</pre></details>
    </article>`;
    })
    .join("\n");

  const stylesheet = path.join(
    workspace,
    "packages/web/src/app/app.styles.css",
  );
  const nativeStyles = await postcss([
    tailwindcss({ base: path.join(workspace, "packages/web") }),
  ]).process(await fs.readFile(stylesheet, "utf8"), { from: stylesheet });
  nativeStyles.root.walkAtRules("layer", (rule) =>
    rule.replaceWith(...(rule.nodes ?? [])),
  );
  nativeStyles.root.walkRules((rule) => {
    if (
      rule.parent.type === "rule" ||
      /keyframes$/.test(rule.parent.name ?? "")
    )
      return;
    rule.selectors = rule.selectors.map((selector) =>
      /^(?::root|:host|html|body)(?=$|[\s.:#[])/.test(selector)
        ? selector.replace(/^(?::root|:host|html|body)/, ".workspace-native")
        : `.workspace-native ${selector}`,
    );
  });
  let nativeCss = nativeStyles.root.toString();
  for (const match of [
    ...nativeCss.matchAll(/url\(["']?(\.\.\/[^)"']+)["']?\)/g),
  ]) {
    const content = await fs.readFile(
      path.resolve(path.dirname(stylesheet), match[1]),
    );
    nativeCss = nativeCss.replaceAll(
      match[0],
      `url(data:image/svg+xml;base64,${content.toString("base64")})`,
    );
  }
  const themeCss = Object.entries(renderers.theme.blueCold.light)
    .map(([key, value]) => `--color-${key}:${value}`)
    .join(";");

  const katexRoot = path.dirname(requireExtra.resolve("katex/package.json"));
  let mathCss = await fs.readFile(
    path.join(katexRoot, "dist/katex.min.css"),
    "utf8",
  );
  for (const match of [...mathCss.matchAll(/url\((fonts\/[^)]+)\)/g)]) {
    const extension = path.extname(match[1]).slice(1);
    const content = await fs.readFile(path.join(katexRoot, "dist", match[1]));
    mathCss = mathCss.replaceAll(
      match[0],
      `url(data:font/${extension};base64,${content.toString("base64")})`,
    );
  }
  const mathLicense = await fs.readFile(
    path.join(katexRoot, "LICENSE"),
    "utf8",
  );
  const html = `<!doctype html>
<!-- KaTeX styles and fonts: ${mathLicense} -->
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>${providerName} ↔ Workspace — конвертация в обе стороны</title><style>${mathCss}\n.telegram-spoiler{background:currentColor;border-radius:3px}.telegram-spoiler:hover,.telegram-spoiler:focus{background:#dce4ee}
:root{color-scheme:light;--ink:#24334a;--muted:#67788d;--border:#dfe6ee;--accent:#4862b3;--surface:#f3f6fa}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--ink);font:14px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#345cb6;text-decoration:none}a:hover{text-decoration:underline}button,input,select{font:inherit}button,summary{cursor:pointer}button:focus-visible,input:focus-visible,a:focus-visible,[role=button]:focus-visible,summary:focus-visible{outline:3px solid #8ba9ef;outline-offset:3px}
.page-header{max-width:1320px;margin:auto;padding:44px 32px 24px}.eyebrow{font-size:11px;font-weight:750;letter-spacing:.14em;color:var(--accent)}h1{font-size:32px;letter-spacing:-1px;line-height:1.25;margin:8px 0 12px}.intro{max-width:900px;color:var(--muted);margin:0}.toolbar{position:sticky;top:0;z-index:2;background:#ffffffed;backdrop-filter:blur(12px);border-block:1px solid var(--border)}.toolbar-inner{max-width:1320px;margin:auto;padding:14px 32px;display:flex;align-items:center;gap:20px}.search{flex:1;min-width:0;max-width:600px;padding:10px 14px;border:1px solid #ccd6e2;border-radius:8px;background:white;color:var(--ink)}.filter{display:flex;align-items:center;gap:8px;white-space:nowrap}.count{margin-left:auto;color:var(--muted);font-size:12px;white-space:nowrap}.container{max-width:1320px;padding:24px 32px 60px;margin:auto}.example{background:white;border:1px solid var(--border);border-radius:12px;margin-bottom:20px;overflow:hidden;scroll-margin-top:95px;box-shadow:0 2px 5px #20304704}.example-heading{padding:18px 22px 0;display:flex;align-items:center;justify-content:space-between;gap:15px}.example-heading>div:first-child{display:flex;align-items:center;gap:10px}.example-heading a{font-size:17px;font-weight:650;color:var(--ink)}.case-number{font-size:11px;color:#8795a6;font-variant-numeric:tabular-nums}.badges{display:flex;gap:6px;flex-wrap:wrap}.badge{font-size:10px;letter-spacing:.015em;background:#f0f3f7;color:#5d7088;padding:3px 9px;border-radius:20px}.badge.unsupported{color:#935627;background:#fff1df}.badge.supported{color:#36735e;background:#edf7f2}.description{padding:0 22px;color:var(--muted);font-size:12px;margin:7px 0 18px}.comparison{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--border)}.column{min-width:0;padding:0 22px 18px}.column+.column{border-left:1px solid var(--border);background:#fbfcff}.column h3{display:flex;align-items:center;gap:7px;font-size:12px;margin:0 -22px 17px;padding:10px 22px;border-bottom:1px solid #edf0f5;color:#4c6077}.column h3 small{font-weight:400;color:#8995a3;margin-left:auto}.brand-dot{width:7px;height:7px;border-radius:3px;background:#4a8ca0}.workspace .brand-dot{background:#6a63b6}.message{padding:0 0 8px}.author{display:flex;align-items:center;gap:9px;margin-bottom:11px;font-size:12px}.author>span:last-child{color:#8c97a6;font-size:10px}.avatar{background:#edf0f8;color:#5f74a5;width:25px;height:25px;border-radius:7px;display:inline-grid;place-items:center;font-size:11px;font-weight:650}.zulip .message-body{margin-left:34px;overflow-wrap:anywhere;min-height:28px;color:#202b3d}.zulip .message-body>:first-child{margin-top:0}.zulip .message-body>:last-child{margin-bottom:0}.zulip .message-body p{margin:0 0 12px;white-space:normal}.zulip .message-body h1,.zulip .message-body h2,.zulip .message-body h3{background:none;border:0;padding:0;margin:15px 0 9px;color:inherit;line-height:1.3;display:block}.zulip .message-body h1{font-size:24px}.zulip .message-body h2{font-size:20px}.zulip .message-body h3{font-size:16px}.zulip .message-body ul,.zulip .message-body ol{padding-left:24px;margin:8px 0 12px}.zulip .message-body li{margin:3px 0}.zulip .message-body pre{margin:10px 0 14px;overflow:auto;white-space:pre;background:#f3f5f8;border:1px solid #e5e9ee;border-radius:6px;padding:12px;font-size:12px;line-height:1.65}.zulip .message-body code{font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:#eef1f5;padding:2px 4px;border-radius:4px}.zulip .message-body pre code{padding:0;background:transparent;white-space:pre}.zulip .message-body blockquote{border-left:3px solid #bdcbdc;background:#f5f7fa;margin:10px 0 14px;padding:9px 13px;color:#53667d}.zulip .message-body blockquote p:last-child{margin-bottom:0}.zulip .message-body table{border-collapse:collapse;max-width:100%;font-size:12px;margin:12px 0}.zulip .message-body th,.zulip .message-body td{border:1px solid #d9e1ea;padding:6px 10px}.zulip .message-body th{background:#eff3f7}.zulip .message-body hr{border:0;border-top:1px solid #d9e1ea;margin:18px 0}.zulip .message-body .user-mention,.zulip .message-body .user-group-mention,.zulip .message-body .topic-mention,.workspace-message-mention{color:#2f6396;background:#e9f2fb;border:0;border-radius:4px;padding:1px 4px;font:inherit;display:inline;line-height:inherit}.zulip .message-body .silent{background:#f0f3f7;color:#667a8e}.workspace-message-reference-link,.stream,.stream-topic{background:#eff1fa;padding:1px 4px;border-radius:4px}.unsupported-placeholder{display:inline-block;color:#96602e;background:#fff3e4;border:1px solid #eedcc4;padding:4px 8px;border-radius:6px;font-size:12px;line-height:1.5;max-width:100%}.spoiler-block{border:1px solid #d9e1ea;border-radius:6px;margin:12px 0;overflow:hidden}.spoiler-header{padding:8px 11px;background:#f0f4f8;font-size:12px;font-weight:600;cursor:pointer}.spoiler-header:before{content:"▸";display:inline-block;margin-right:8px}.spoiler-header[aria-expanded=true]:before{content:"▾"}.spoiler-content{display:none;padding:12px}.spoiler-block.is-open>.spoiler-content{display:block}.spoiler-content>p:last-child{margin-bottom:0}.zulip .message-body .emoji{font-size:17px;line-height:1;vertical-align:baseline}.zulip .message-body img.emoji{width:20px;height:20px;object-fit:cover;vertical-align:middle}.zulip .message-body img:not(.emoji){max-width:100%;height:auto;display:block;border-radius:7px}.fixture-media,.workspace-message-file-placeholder{display:block;max-width:380px;width:100%;margin:9px 0 13px;border-radius:8px;overflow:hidden;border:1px solid #dce4ee;background:#eef3f8}.workspace-message-file-placeholder__label.sr-only,.workspace-message-file-placeholder__video-visual{display:none}.fixture-attachment{display:inline-flex;align-items:center;gap:8px;max-width:100%;padding:8px 12px;border:1px solid #d9e2ed;border-radius:7px;background:#f4f7fb;color:#3d597b;font-size:12px;margin:5px 0}.fixture-attachment:before{content:"↧";font-size:17px;color:#8195ae}.action-message{font-style:italic}.widget{border:1px solid #dce4ee;border-radius:8px;padding:14px;background:#fbfcfd}.widget-label{font-size:9px;letter-spacing:.08em;color:#8292a5;margin-bottom:6px}.widget ul{list-style:none;margin:12px 0 0;padding:0}.widget li{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #e7edf4}.widget li b{margin-left:auto;font-size:12px;color:#5b6e87}.poll-circle{width:14px;height:14px;border-radius:50%;border:1px solid #b9c8d8;flex-shrink:0}.widget small{display:block;color:var(--muted);font-size:11px}.completed{text-decoration:line-through;color:#8795a5}.zulip .message-body time{border-bottom:1px dotted #6c7ba3;color:#4868aa}.zulip .message-body .katex-display{overflow:auto;padding:4px}.workspace-message-gap{display:block;height:8px}.workspace-message-gap--2{height:16px}.workspace-message-gap--3{height:24px}.feature-note{font-size:10px;color:#9a7244;background:#fffbf5;padding:8px 22px;border-top:1px solid #f1e8da}.column details{margin-left:34px;margin-top:15px;font-size:11px;color:#7f8b9b}.column details pre,.fixture-data pre{font:11px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;background:#f0f3f7;color:#4f6177;overflow:auto;padding:12px;border-radius:6px;max-height:420px;white-space:pre-wrap}.fixture-data{padding:8px 22px;border-top:1px solid #edf0f5;color:#8793a3;font-size:10px}.empty-state{text-align:center;color:var(--muted);padding:50px}.footer{text-align:center;color:#8c98a8;font-size:11px;padding:0 20px 28px}[hidden]{display:none!important}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.spoiler-header>p{display:inline;margin:0}
@media(max-width:760px){.page-header{padding:28px 18px 22px}h1{font-size:26px}.toolbar-inner{padding:12px 18px;gap:12px;flex-wrap:wrap}.search{flex-basis:100%;max-width:none}.filter{font-size:12px}.count{font-size:11px}.container{padding:18px 12px 35px}.comparison{grid-template-columns:1fr}.column+.column{border-left:0;border-top:1px solid var(--border)}.example-heading{align-items:flex-start;padding:15px 16px 0}.example-heading a{font-size:14px}.badges{justify-content:flex-end}.description{padding:0 16px}.column{padding-inline:16px}.column h3{margin-inline:-16px;padding-inline:16px}.zulip .message-body{margin-left:0}.column details{margin-left:0}.badge{font-size:9px}.feature-note{padding-inline:16px}}
@media print{.toolbar,.column details,.fixture-data{display:none}.example{break-inside:avoid;box-shadow:none}.spoiler-content{display:block}.container{padding:0}.page-header{padding-top:0}.comparison{grid-template-columns:1fr 1fr}}
/* Native Workspace CSS is scoped to the preview and compiled from workspace_ui. */
${nativeCss}
.workspace-native{${themeCss};margin-left:34px;font-size:14px;line-height:20px;color:var(--color-text-primary)}
.workspace-native .fixture-bubble{border-radius:18px 18px 6px 18px;padding:8px 12px;background:var(--color-msg-bg);box-shadow:0 1px 3px #0002;max-width:100%;width:fit-content}
.workspace-native .fixture-bubble[data-message-owner=own]{background:var(--color-msg-own-bg)}
.workspace-native .fixture-bubble[data-message-owner=peer]{border-radius:18px 18px 18px 6px}
.workspace-native .fixture-time{text-align:right;font-size:10px;line-height:14px;color:var(--color-msg-time);margin-top:3px}
@media(max-width:760px){.workspace-native{margin-left:0}}
</style></head><body>
<header class="page-header"><div class="eyebrow">MESSAGE CONVERSION FIXTURES</div><h1>${providerName} <span style="color:#9aa7bb">↔</span> Workspace — конвертация в обе стороны</h1><p class="intro">${examples.length} examples. ${telegram ? "Telegram columns illustrate MTProto entity semantics; they are not a Telegram client screenshot. Workspace uses its actual local message renderer." : "Rendered with the local Zulip client and Workspace message renderers."} Workspace uses its native quote and attachment components, message styles and Blue Cold light theme. Media uses synthetic previews; polls and to-do lists are read-only snapshots. Open the source below each message to compare the exact Markdown.</p></header>
<div class="toolbar"><div class="toolbar-inner"><label for="search" class="sr-only">Search examples</label><input id="search" class="search" type="search" placeholder="Search names, content or Markdown…"><label class="filter"><input id="unsupported" type="checkbox"> Unsupported only</label><output id="count" class="count" aria-live="polite">${examples.length} of ${examples.length} examples</output></div></div>
<main class="container">${cards}<p id="empty" class="empty-state" hidden>No examples match your search.</p></main>
<footer class="footer">Offline preview · No API requests · Fictional users and files · Built from ${fixtureName}.json</footer>
<script>
const examples = Array.from(document.querySelectorAll('.example'));
function filterExamples() {
  const query = document.querySelector('#search').value.toLowerCase().trim();
  const unsupportedOnly = document.querySelector('#unsupported').checked;
  let visible = 0;
  for (const example of examples) {
    example.hidden = !example.dataset.search.includes(query) || (unsupportedOnly && example.dataset.unsupported !== 'true');
    if (!example.hidden) visible++;
  }
  document.querySelector('#count').textContent = visible + ' of ' + examples.length + ' examples';
  document.querySelector('#empty').hidden = visible !== 0;
}
document.querySelector('#search').addEventListener('input', filterExamples);
document.querySelector('#unsupported').addEventListener('change', filterExamples);
function toggleSpoiler(header) {
  const block = header.parentElement;
  const open = block.classList.toggle('open');
  block.classList.toggle('is-open', open);
  header.setAttribute('aria-expanded', String(open));
  block.querySelector(':scope > .spoiler-content').setAttribute('aria-hidden', String(!open));
}
document.addEventListener('click', event => {
  const header = event.target.closest('.spoiler-header');
  if (header) toggleSpoiler(header);
});
document.addEventListener('keydown', event => {
  if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('.spoiler-header')) {
    event.preventDefault(); toggleSpoiler(event.target);
  }
});
</script></body></html>`;
  const output = process.env.REVIEW_RENDER_OUTPUT ?? path.join(directory, `${fixtureName}_preview.html`);
  await fs.writeFile(output, roundtripRendered
    ? JSON.stringify({cases:roundtripRendered, styles:html.match(/<style>([\s\S]*?)<\/style>/)[1]})
    : html.replace(/^[ \t]+$/gm, ""));
  console.log(
    `Validated ${urnCount} Workspace URNs; rendered ${examples.length} comparison cards: ${output}`,
  );
} finally {
  dom.window.close();
  await fs.rm(temporary, { recursive: true, force: true });
}
