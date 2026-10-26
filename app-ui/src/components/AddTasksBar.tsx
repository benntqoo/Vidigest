// 添加链接的输入区（受控）。
//
// 支持一次粘贴多条：整段抖音分享文案直接贴进来，抽 URL 的活由 Python 侧的
// `urls.extract_many` 干（`PLAN §7.3`）—— 前端不做正则，避免两处规则打架。
//
// 2026-10-27 改成受控：空队列时「添加链接」按钮在列表中间，而展开状态原来
// 只能由这一行自己切换，于是空态那个按钮只能自己另开一个输入框 —— 两个入口
// 各持一份文本，用户会丢掉已经打了一半的内容。

import { useState } from "react";

interface Props {
  onSubmit: (text: string) => void;
  disabled: boolean;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}

export function AddTasksBar({ onSubmit, disabled, open, onOpenChange }: Props) {
  const [text, setText] = useState("");

  const submit = () => {
    const v = text.trim();
    if (!v) return;
    onSubmit(v);
    setText("");
    onOpenChange(false);
  };

  if (!open) {
    return (
      <button className="primary" onClick={() => onOpenChange(true)} disabled={disabled}>
        添加链接
      </button>
    );
  }

  return (
    <div className="addtasks">
      <textarea
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={
          "每行一条，或直接粘贴整段分享文案。示例：\n" +
          "https://v.douyin.com/AbCdEf12345/\n" +
          "8.25 复制打开抖音，看看【某创作者的作品】… https://v.douyin.com/XyZ987/ 干扰码"
        }
        rows={4}
        onKeyDown={(e) => {
          // Ctrl/Cmd + Enter 提交；Esc 取消
          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submit();
          if (e.key === "Escape") onOpenChange(false);
        }}
      />
      <div className="addtasks-actions">
        <span className="muted">Ctrl+Enter 提交 · Esc 取消 · 本地文件路径同样支持</span>
        <span className="spacer" />
        <button className="mini" onClick={() => onOpenChange(false)}>
          取消
        </button>
        <button className="primary" onClick={submit} disabled={!text.trim()}>
          加入队列
        </button>
      </div>
    </div>
  );
}
