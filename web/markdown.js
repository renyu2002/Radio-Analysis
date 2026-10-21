/*!
 * markdown.js —— 视频分析 · DeepSeek 内置迷你 Markdown 渲染器
 * 无依赖、离线可用（不引任何 CDN）。仅支持总结/对话所需的安全子集：
 * 标题、段落、加粗/斜体/删除线、行内代码、代码块、有序/无序列表（含一级嵌套）、
 * 引用、分割线、表格、链接、【MM:SS】时间戳（渲染为可点击跳转锚点）。
 * 所有文本先转义再渲染，绝不注入原始 HTML。
 */
(function (global) {
  'use strict';

  var ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  var PLACEHOLDER = '\u0000';

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return ESCAPE_MAP[ch];
    });
  }

  function safeUrl(url) {
    var trimmed = String(url || '').trim();
    return /^https?:\/\//i.test(trimmed) ? trimmed : '';
  }

  function timestampToSeconds(a, b, c) {
    if (c == null) return Number(a) * 60 + Number(b);
    return Number(a) * 3600 + Number(b) * 60 + Number(c);
  }

  /**
   * 行内渲染。处理顺序：先抽取「行内代码 / 链接 / 时间戳 / 裸链接」为占位符，
   * 再对整个字符串转义，然后处理强调语法，最后还原占位符。
   */
  function inline(text) {
    var tokens = [];
    function stash(html) {
      tokens.push(html);
      return PLACEHOLDER + (tokens.length - 1) + PLACEHOLDER;
    }

    var src = String(text == null ? '' : text);

    // 1. 行内代码
    src = src.replace(/`([^`\n]+)`/g, function (m, code) {
      return stash('<code class="md-code-inline">' + escapeHtml(code) + '</code>');
    });

    // 2. Markdown 链接 [文字](http...)
    src = src.replace(/\[([^\[\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, function (m, label, url) {
      var href = safeUrl(url);
      if (!href) return m;
      return stash('<a class="md-link" href="' + escapeHtml(href) + '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(label) + '</a>');
    });

    // 3. 【MM:SS】/【H:MM:SS】时间戳 → 可点击跳转
    src = src.replace(/【(\d{1,2}):([0-5]\d)(?::([0-5]\d))?】/g, function (m, a, b, c) {
      var seconds = timestampToSeconds(a, b, c);
      return stash('<a class="ts-link" href="#" data-seek="' + seconds + '" title="点击跳转到 ' +
        escapeHtml(m) + '">' + escapeHtml(m) + '</a>');
    });

    // 4. 裸链接自动识别
    src = src.replace(/(^|[\s(（【>])(https?:\/\/[^\s<>()（）【】]+)/g, function (m, prefix, url) {
      var href = safeUrl(url);
      if (!href) return m;
      return prefix + stash('<a class="md-link" href="' + escapeHtml(href) +
        '" target="_blank" rel="noopener noreferrer">' + escapeHtml(url) + '</a>');
    });

    src = escapeHtml(src);

    // 5. 强调（转义后 * 与 ~ 仍然保留）
    src = src.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    src = src.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    src = src.replace(/(^|[^*])\*([^*\n]+)\*/g, function (m, prefix, body) {
      return prefix + '<em>' + body + '</em>';
    });
    src = src.replace(/~~([^~]+)~~/g, '<del>$1</del>');

    // 6. 还原占位符
    src = src.replace(/\u0000(\d+)\u0000/g, function (m, idx) {
      return tokens[Number(idx)] || '';
    });

    return src;
  }

  function isTableSeparator(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(line);
  }

  function splitTableRow(line) {
    var trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return trimmed.split('|').map(function (cell) { return cell.trim(); });
  }

  function buildTable(header, rows) {
    var thead = '<thead><tr>' + header.map(function (cell) {
      return '<th>' + inline(cell) + '</th>';
    }).join('') + '</tr></thead>';
    var tbody = '<tbody>' + rows.map(function (row) {
      var cells = [];
      for (var i = 0; i < header.length; i++) {
        cells.push('<td>' + inline(row[i] == null ? '' : row[i]) + '</td>');
      }
      return '<tr>' + cells.join('') + '</tr>';
    }).join('') + '</tbody>';
    return '<div class="md-table-wrap"><table class="md-table">' + thead + tbody + '</table></div>';
  }

  function listItem(line) {
    var m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (!m) return null;
    return {
      indent: m[1].replace(/\t/g, '  ').length,
      ordered: /\d/.test(m[2]),
      text: m[3]
    };
  }

  function collectListItems(lines, start) {
    var items = [];
    var i = start;
    while (i < lines.length) {
      var item = listItem(lines[i]);
      if (!item) break;
      var text = item.text;
      i++;
      // 列表项续行（缩进的非列表行并入当前项）
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !listItem(lines[i]) && /^\s{2,}/.test(lines[i])) {
        text += '\n' + lines[i].trim();
        i++;
      }
      items.push({ indent: item.indent, ordered: item.ordered, text: text });
    }
    return { items: items, next: i };
  }

  function buildList(items, baseIndent) {
    if (!items.length) return '';
    var indent = baseIndent == null ? items[0].indent : baseIndent;
    var ordered = null;
    var html = '';
    var i = 0;
    while (i < items.length) {
      var item = items[i];
      if (item.indent < indent) break;
      if (ordered === null) ordered = item.ordered;
      // 收集属于当前项的嵌套子项
      var nested = [];
      var j = i + 1;
      while (j < items.length && items[j].indent > item.indent) {
        nested.push(items[j]);
        j++;
      }
      var body = inline(item.text).replace(/\n/g, '<br>');
      html += '<li>' + body + (nested.length ? buildList(nested, nested[0].indent) : '') + '</li>';
      i = j;
    }
    var tag = ordered ? 'ol' : 'ul';
    return '<' + tag + ' class="md-list">' + html + '</' + tag + '>';
  }

  function isBlockStart(lines, i) {
    var line = lines[i];
    return /^\s*(`{3,}|~{3,})/.test(line) ||
      /^#{1,6}\s+/.test(line) ||
      /^\s*>/.test(line) ||
      /^\s*([-*+]|\d+[.)])\s+/.test(line) ||
      /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line) ||
      (line.indexOf('|') >= 0 && i + 1 < lines.length && isTableSeparator(lines[i + 1]));
  }

  /**
   * 渲染 Markdown 为 HTML 字符串。
   * @param {string} markdown
   * @returns {string}
   */
  function render(markdown) {
    var lines = String(markdown == null ? '' : markdown).replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (/^\s*$/.test(line)) { i++; continue; }

      // 代码块
      var fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/);
      if (fence) {
        var closeRe = new RegExp('^\\s*' + (fence[1].charAt(0) === '`' ? '`{3,}' : '~{3,}') + '\\s*$');
        var code = [];
        i++;
        while (i < lines.length && !closeRe.test(lines[i])) { code.push(lines[i]); i++; }
        if (i < lines.length) i++;
        out.push('<pre class="md-pre"><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
        continue;
      }

      // 标题
      var heading = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        var level = Math.min(heading[1].length, 6);
        out.push('<h' + level + ' class="md-heading md-h' + level + '">' + inline(heading[2]) + '</h' + level + '>');
        i++;
        continue;
      }

      // 分割线
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        out.push('<hr class="md-hr">');
        i++;
        continue;
      }

      // 引用
      if (/^\s*>/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        out.push('<blockquote class="md-quote">' + render(quote.join('\n')) + '</blockquote>');
        continue;
      }

      // 表格
      if (line.indexOf('|') >= 0 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        var headerCells = splitTableRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf('|') >= 0 && !/^\s*$/.test(lines[i])) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        out.push(buildTable(headerCells, rows));
        continue;
      }

      // 列表
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        var collected = collectListItems(lines, i);
        out.push(buildList(collected.items));
        i = collected.next;
        continue;
      }

      // 段落
      var paragraph = [line];
      i++;
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !isBlockStart(lines, i)) {
        paragraph.push(lines[i]);
        i++;
      }
      out.push('<p class="md-p">' + inline(paragraph.join('\n')).replace(/\n/g, '<br>') + '</p>');
    }
    return out.join('\n');
  }

  function renderInline(text) {
    return inline(text);
  }

  var api = {
    render: render,
    renderInline: renderInline,
    escapeHtml: escapeHtml
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  global.Markdown = api;
})(typeof window !== 'undefined' ? window : globalThis);
