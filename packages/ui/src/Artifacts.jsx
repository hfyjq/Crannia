// 助手一条回复产出的东西（图 / 音 / 视频）的展示组件。
//
// 形状来自 `core/artifacts.py` 的记录：
//   {"id":"a1b2c3d4e5f6a7b8.png","kind":"image","name":"销售趋势.png",
//    "mime":"image/png","size":12345,"origin":"tool|model"}
//
// 只拿 `id` 拼地址，不拼磁盘路径 —— 端点是 `GET /api/artifact?id=…`，
//   它也只认 id（`core/artifacts.py::valid_id`）。这是"一个口径一个地方"那条。
//
// token 走查询串：`<img>` / `<audio>` / `<video>` 发不了自定义 header
//   （与 `api.js::sseUrl` 逐字同一条理由）。拿 token 是异步的，每条自己取一次
//   （`image-viewer.jsx::useArtifactUrl`）；`client.artifactUrl` 内部缓存 token，
//   不会真打 N 次 `/ping`。
//
// 图片不能用 `innerHTML` / data URL 塞进 DOM：这些是工具或模型产出的
//   字节，不是我们的模板 —— `<img src>` 让浏览器自己解码，脚本根本不会跑。

import { useState } from "react";

import { t } from "./i18n.js";
//:展示判据住在 `artifacts-view.js`（纯函数，node 真跑）：
//:   "能不能内联画/播"与"够不够格弹大文件通知"都在那里，别在 JSX 里再判一次。
import { formatSize, isInline } from "./artifacts-view.js";
//:大图查看器与用户附件共用一份（`image-viewer.jsx`：portal + 左右翻页
//:   + Esc/点遮罩关 + 焦点还原）。两处各写一份，迟早一处能翻、一处不能。
import { ImageViewer, useArtifactUrl } from "./image-viewer.jsx";

/** 超过内联闸的产物：一行下载。
 *
 *  判据是"浏览器要不要一次读完"（`core/artifacts.py` 的 `inline`）。
 *    塞 `<video>` 进去的代价不是慢：大文件在聊天区里解不出来，用户
 *    看到的是一个永远转圈的黑框 —— 比一行下载更糟。
 */
function DownloadArtifact({ rec, client }) {
  const url = useArtifactUrl(client, rec.id);
  if (!url) return <div className="artifact-loading">{t("正在取回…")}</div>;
  const name = rec.name || rec.id;
  const size = formatSize(rec.size);
  return (
    <a className="artifact-file" href={url} download={name} title={name}>
      <span className="artifact-file-name">{name}</span>
      {size ? <span className="artifact-file-size">{size}</span> : null}
    </a>
  );
}

function ImageArtifact({ rec, client, onOpen }) {
  const url = useArtifactUrl(client, rec.id);
  return (
    <button type="button" className="artifact-thumb" title={rec.name || rec.id}
            onClick={() => onOpen(rec)}>
      {url ? <img src={url} alt={rec.name || t("产物")} loading="lazy" /> : null}
    </button>
  );
}

function MediaArtifact({ rec, client }) {
  const url = useArtifactUrl(client, rec.id);
  if (!url) return <div className="artifact-loading">{t("正在取回…")}</div>;
  if (rec.kind === "audio") {
    return <audio className="artifact-media" controls preload="metadata"
                  src={url} title={rec.name || rec.id} />;
  }
  if (rec.kind === "video") {
    return <video className="artifact-media" controls preload="metadata"
                  src={url} title={rec.name || rec.id} />;
  }
  return null;
}

export default function Artifacts({ records, client }) {
  //: 开着的是第几张（不是那一条记录）—— 查看器要能左右翻，
  //:   所以它收的是整组 `items` + 一个下标。
  const [openAt, setOpenAt] = useState(-1);
  const items = (Array.isArray(records) ? records : [])
    .filter((r) => r && typeof r === "object" && r.id);

  if (!items.length) return null;

  // 先按内联闸分家：`inline: false` 的一律走下载行。
  //   分组照旧按 kind —— 只是"能不能内联"这一层在它们前面。
  const files = items.filter((r) => !isInline(r));
  const inline = items.filter(isInline);
  const images = inline.filter((r) => r.kind === "image");
  const media = inline.filter((r) => r.kind === "audio" || r.kind === "video");

  return (
    <div className="artifacts">
      {images.length ? (
        <div className={"artifact-grid" + (images.length === 1 ? " single" : "")}>
          {images.map((r, i) => (
            <ImageArtifact key={r.id} rec={r} client={client}
                           onOpen={() => setOpenAt(i)} />
          ))}
        </div>
      ) : null}
      {media.map((r) => <MediaArtifact key={r.id} rec={r} client={client} />)}
      {files.map((r) => <DownloadArtifact key={r.id} rec={r} client={client} />)}

      {/* 大图查看器与用户附件共用一份（`image-viewer.jsx`）：
          portal 到 body、Esc/点遮罩关、左右环绕翻页、焦点还原。 */}
      {openAt >= 0 ? (
        <ImageViewer items={images} index={openAt} client={client}
                     onClose={() => setOpenAt(-1)} />
      ) : null}
    </div>
  );
}
