import type { Metadata } from "next";
import { DashboardClient } from "./dashboard-client";

export const metadata: Metadata = {
  title: "任务总览 · Lab Task Console",
  description: "查看实验群任务进度、临期风险与负责人分布。",
};

export default function Home() {
  return <DashboardClient />;
}
