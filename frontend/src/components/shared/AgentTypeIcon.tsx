import { Bot, ShieldCheck, Terminal, Layers } from 'lucide-react';
import type { AgentType } from '@/types/dashboard';

interface Props {
  type: AgentType;
  size?: number;
  className?: string;
}

/** Lucide icon matching the conductor web app's agent type icons */
export function AgentTypeIcon({ type, size = 14, className }: Props) {
  const props = { size, className };
  switch (type) {
    case 'human_gate': return <ShieldCheck {...props} />;
    case 'script': return <Terminal {...props} />;
    case 'workflow': return <Layers {...props} />;
    default: return <Bot {...props} />;
  }
}
