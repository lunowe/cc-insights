import { Monitor, Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useTheme, type ThemeChoice } from "@/hooks/use-theme"

const LABEL: Record<ThemeChoice, string> = {
  system: "Matching system",
  light: "Light",
  dark: "Dark",
}

export function ThemeToggle() {
  const { choice, cycle } = useTheme()
  const Icon = choice === "system" ? Monitor : choice === "light" ? Sun : Moon

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          onClick={cycle}
          aria-label={`Theme: ${LABEL[choice]}. Click to change.`}
        >
          <Icon />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{LABEL[choice]}</TooltipContent>
    </Tooltip>
  )
}
