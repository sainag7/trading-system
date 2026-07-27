import {
  LayoutDashboard, Lightbulb, Search, Wallet, TrendingUp, CalendarClock, Settings2, History,
} from "lucide-react";

export type View =
  | "overview" | "ideas" | "deepdive" | "portfolio" | "trade"
  | "automation" | "activity" | "settings";

export const NAV: { id: View; label: string; icon: any }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "ideas", label: "Ideas", icon: Lightbulb },
  { id: "deepdive", label: "Deep dive", icon: Search },
  { id: "portfolio", label: "Portfolio", icon: Wallet },
  { id: "trade", label: "Trade", icon: TrendingUp },
  { id: "automation", label: "Automation", icon: CalendarClock },
  { id: "activity", label: "Activity", icon: History },
  { id: "settings", label: "Settings", icon: Settings2 },
];

export const TITLES: Record<View, string> = {
  overview: "Overview",
  ideas: "Ideas",
  deepdive: "Deep dive",
  portfolio: "Portfolio",
  trade: "Trade",
  automation: "Automation",
  activity: "Activity",
  settings: "Settings",
};
