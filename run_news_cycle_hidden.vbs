Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\AgentPC\source\repos\polymarket-lab"
sh.Run """C:\Users\AgentPC\source\repos\polymarket-lab\run_news_cycle.cmd""", 0, False
