import DOMPurify from "dompurify";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Bold, Heading2, Italic, List, ListOrdered, Redo2, Undo2 } from "lucide-react";
import { type ReactNode, useEffect } from "react";

type Props = {
  value: string;
  onChange: (value: string) => void;
};

export function HtmlNoteEditor({ value, onChange }: Props) {
  const editor = useEditor({
    extensions: [StarterKit],
    content: value,
    editorProps: { attributes: { class: "note-editor-content np-note-theme" } },
    onUpdate: ({ editor: current }) => onChange(current.getHTML())
  });

  useEffect(() => {
    if (editor && value !== editor.getHTML()) editor.commands.setContent(value, false);
  }, [editor, value]);

  if (!editor) return null;
  const tool = (label: string, active: boolean, action: () => void, icon: ReactNode) => (
    <button type="button" className={active ? "active" : ""} title={label} aria-label={label} onClick={action}>{icon}</button>
  );
  return <div className="rich-note-editor">
    <div className="rich-note-toolbar">
      {tool("撤销", false, () => editor.chain().focus().undo().run(), <Undo2 size={16}/>)}
      {tool("重做", false, () => editor.chain().focus().redo().run(), <Redo2 size={16}/>)}
      {tool("粗体", editor.isActive("bold"), () => editor.chain().focus().toggleBold().run(), <Bold size={16}/>)}
      {tool("斜体", editor.isActive("italic"), () => editor.chain().focus().toggleItalic().run(), <Italic size={16}/>)}
      {tool("二级标题", editor.isActive("heading", { level: 2 }), () => editor.chain().focus().toggleHeading({ level: 2 }).run(), <Heading2 size={16}/>)}
      {tool("无序列表", editor.isActive("bulletList"), () => editor.chain().focus().toggleBulletList().run(), <List size={16}/>)}
      {tool("有序列表", editor.isActive("orderedList"), () => editor.chain().focus().toggleOrderedList().run(), <ListOrdered size={16}/>)}
    </div>
    <EditorContent editor={editor}/>
  </div>;
}

export function HtmlNotePreview({ html }: { html: string }) {
  return <article
    className="html-note-preview np-note-theme"
    dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(html) }}
  />;
}
