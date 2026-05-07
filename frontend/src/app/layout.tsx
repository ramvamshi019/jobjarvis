import type { Metadata } from "next";
import "./globals.css";
import { AuthProvider } from "@/contexts/AuthContext";
import NavBar from "@/components/NavBar";

export const metadata: Metadata = {
  title: "JobJarvis — Find Tech Jobs",
  description:
    "Search thousands of real data engineering and tech jobs, " +
    "updated every 5 minutes from top company career pages.",
  openGraph: {
    title: "JobJarvis — Find Tech Jobs",
    description: "Real-time tech job search powered by AI.",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <NavBar />
          {children}
        </AuthProvider>
      </body>
    </html>
  );
}
