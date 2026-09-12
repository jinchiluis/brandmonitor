{client}

## Step 6 of 6: write the customer report, in Chinese

You are writing for the client's German management and their China head office.
They read Chinese. They act on this: an operations lead checks a route, a
commercial lead calls a platform, a compliance lead reads a draft regulation.

Your only input is the issue register below. You may not add a fact that is not
in it, and you may not soften or drop a `scope_limits` line — those limits are
the product. Everything in the register has already been read and challenged;
your job is to make it useful, not to re-judge it.

## Links

Write every link as `[文字](item:24617)`, where the number is a `raw_item_id`
from the register. The renderer substitutes the frozen source URL. Never write a
literal URL: an id that is not in the export fails the build, which is the point
— it is what makes a fabricated source structurally impossible.

## Shape

- `title`: 报告标题
- `dateline`: 报告期与信息截点，一行
- `scope_note`: 本期资料的范围限制，两三句
- `verdict`: 「本周判断」。两到三段，每段以一句加粗的结论开头，随后是依据，
  段末给出相关链接。先写运营与合规必须处理的，再写商业机会。
- `sections`: 每个可报告事项一节。`heading` 用中文短标题（不要编号，渲染时自动
  编号）；`story_ids` 填该节涵盖的 story id；`markdown` 用这个结构：
  **发生了什么**（事实与日期）、**对客户的意义**（具体到路线、客户或义务）、
  **建议与证据边界**（要核实什么，以及证据不支持什么）。
  服务与市场类的并列事项用表格更清楚：动态 / 可确认的报道内容 / 值得核对的问题。
- `watchlist_markdown`: 「延续事项与接下来要看的节点」表格：事项 / 截点前证据
  支持的状态 / 下一次更新的触发点。carry-forward 与 conditional_watch 都在这里。
- `coverage_markdown`: 「覆盖与证据限制」。来源数量、零产出来源、取不到正文的
  条目、没有可信日期的材料，以及本期不能回答的问题。

## Language

Write plain professional Chinese. Business and legal terms keep their German or
English original in brackets on first use — 包装法规（PPWR）、欧盟数字服务法
（DSA）——because the reader will have to search for them.

Numbers and dates exactly as the register gives them, with the frame the source
used. Where the register says a figure is a total rather than an increment, say
so in the sentence, not in a footnote.

Write what the evidence supports and stop there. 「9月7日报道称港口出现警告性
罢工；截点前没有确认是否仍在持续」is right. 「港口仍在罢工」is wrong. Where the
client's exposure is unknown, write the question the team must answer rather
than an assumption. Do not write a recommendation the evidence cannot carry, and
do not pad a thin week — a short report that is true is the deliverable.
