import React, { useEffect, useState } from "react";
//:形状与颜色只有一份，住在 `avatar.js`（纯函数，node 能直接断言）。
//:这个文件只负责画 —— 别在这儿再写一遍 `SHAPES`（两份会漂）。
import { avatarFor } from "./avatar.js";

//: 8 个槽位 → 8 张 160×160 的圆形 PNG。
//:   素材由 `scripts/prepare_avatars.py` 纯代码生成，无外部素材。
//:静态 import 是必须的（Vite 才会把它们打进包、并把 URL 换成构建后的
//:   相对路径）—— 写 `"/assets/..."` 会撞 `test_desktop_assets.py` 那条
//:   「资源路径只许出现在 `client.asset(...)` 里」。
//:图片映射放这里、不放 `avatar.js`：那个文件要被 node 直接 import
//:   （`packages/ui/tests/avatar.test.mjs`），node 认不得 PNG。
import slot0 from "./assets/avatar/slot-0.png";
import slot1 from "./assets/avatar/slot-1.png";
import slot2 from "./assets/avatar/slot-2.png";
import slot3 from "./assets/avatar/slot-3.png";
import slot4 from "./assets/avatar/slot-4.png";
import slot5 from "./assets/avatar/slot-5.png";
import slot6 from "./assets/avatar/slot-6.png";
import slot7 from "./assets/avatar/slot-7.png";

const IMAGES = [slot0, slot1, slot2, slot3, slot4, slot5, slot6, slot7];

// 群成员头像。
//
// 槽位是键：后端存 `0..7`（`core/groups.py`），真头像照同一槽位换
//   —— 接口不变。
// 有兜底：图裂了（文件没进包 / 构建路径错）就退回纯色几何图形。
//   判据：坏掉要看得见，不能留一个空圆圈（那看起来像"这个人没有头像"）。
//
// 不用 `dangerouslySetInnerHTML`：形状是数据表，这里拿它 `createElement`
//   —— 注入面为零。
//
// `size` 给的是像素，`viewBox` 恒为 24×24（形状表就是按它画的）——
//   缩放靠 viewBox，不靠改 `SHAPES` 里的坐标。

export function Avatar({ slot, name = "", size = 28, title = "" }) {
  const { shape, color, slot: i } = avatarFor(slot);
  //:图裂了就切回 SVG；槽位一变就重置（下一张图可能是好的）。
  const [broken, setBroken] = useState(false);
  useEffect(() => { setBroken(false); }, [i]);

  return (
    <span className="avatar" title={title || name || ""}
          data-slot={i} data-shape={shape.name}
          data-image={broken ? "fallback" : "photo"}
          style={{ width: size, height: size }}>
      {broken ? (
        <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
          {React.createElement(shape.el, { ...shape.attrs, fill: color })}
        </svg>
      ) : (
        <img src={IMAGES[i]} width={size} height={size} alt=""
             draggable={false} onError={() => setBroken(true)} />
      )}
    </span>
  );
}
