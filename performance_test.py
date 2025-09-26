#!/usr/bin/env python3
"""
Kubernetes Web终端性能测试脚本
用于测试多终端并发操作的性能表现
"""

import asyncio
import time
import json
import websockets
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor
import argparse
import sys


class TerminalPerformanceTester:
    def __init__(self, base_url, namespace="default", pod_name="test-pod"):
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.pod_name = pod_name
        self.results = []
        self.lock = threading.Lock()

    async def test_single_terminal(self, terminal_id, test_duration=30):
        """测试单个终端的性能"""
        ws_url = (
            f"{self.base_url.replace('http', 'ws')}/ws/{self.namespace}/{self.pod_name}"
        )

        try:
            async with websockets.connect(ws_url) as websocket:
                print(f"终端 {terminal_id}: 连接成功")

                # 性能指标
                message_count = 0
                response_times = []
                error_count = 0
                start_time = time.time()
                last_activity = start_time

                # 发送测试命令
                test_commands = [
                    "echo 'Performance test started'\n",
                    "ls -la /\n",
                    "ps aux\n",
                    "df -h\n",
                    "cat /etc/passwd\n",
                    "find /usr -name '*.conf' | head -10\n",
                    "echo 'Test data' && for i in {1..100}; do echo 'Line $i'; done\n",
                ]

                command_index = 0

                while time.time() - start_time < test_duration:
                    try:
                        # 发送测试命令
                        if command_index < len(test_commands):
                            command = test_commands[command_index]
                            send_time = time.time()
                            await websocket.send(command)
                            command_index += 1

                        # 接收响应
                        response_start = time.time()
                        try:
                            response = await asyncio.wait_for(
                                websocket.recv(), timeout=5.0
                            )
                            response_time = time.time() - response_start

                            if response:
                                message_count += 1
                                response_times.append(response_time)
                                last_activity = time.time()

                                # 记录响应时间（每10条消息记录一次）
                                if message_count % 10 == 0:
                                    print(
                                        f"终端 {terminal_id}: 收到消息 {message_count}, "
                                        f"响应时间: {response_time:.3f}s"
                                    )

                        except asyncio.TimeoutError:
                            print(f"终端 {terminal_id}: 接收超时")
                            error_count += 1

                    except Exception as e:
                        print(f"终端 {terminal_id}: 错误 - {e}")
                        error_count += 1
                        await asyncio.sleep(1)

                    # 小延迟避免过快
                    await asyncio.sleep(0.1)

                # 计算性能指标
                test_time = time.time() - start_time

                result = {
                    "terminal_id": terminal_id,
                    "test_time": test_time,
                    "message_count": message_count,
                    "error_count": error_count,
                    "avg_response_time": (
                        statistics.mean(response_times) if response_times else 0
                    ),
                    "max_response_time": max(response_times) if response_times else 0,
                    "min_response_time": min(response_times) if response_times else 0,
                    "messages_per_second": (
                        message_count / test_time if test_time > 0 else 0
                    ),
                    "success_rate": (
                        (message_count / (message_count + error_count) * 100)
                        if (message_count + error_count) > 0
                        else 0
                    ),
                }

                with self.lock:
                    self.results.append(result)

                print(
                    f"终端 {terminal_id}: 测试完成 - "
                    f"消息数: {message_count}, "
                    f"错误: {error_count}, "
                    f"平均响应时间: {result['avg_response_time']:.3f}s"
                )

                return result

        except Exception as e:
            print(f"终端 {terminal_id}: 连接失败 - {e}")
            error_result = {
                "terminal_id": terminal_id,
                "test_time": 0,
                "message_count": 0,
                "error_count": 1,
                "avg_response_time": 0,
                "max_response_time": 0,
                "min_response_time": 0,
                "messages_per_second": 0,
                "success_rate": 0,
                "connection_error": str(e),
            }

            with self.lock:
                self.results.append(error_result)

            return error_result

    def run_concurrent_test(self, num_terminals, test_duration=30):
        """运行并发测试"""
        print(f"开始并发测试: {num_terminals} 个终端, 测试时长: {test_duration}秒")
        print(f"WebSocket URL: {self.base_url}/ws/{self.namespace}/{self.pod_name}")

        start_time = time.time()

        # 创建事件循环
        async def run_all_tests():
            tasks = []
            for i in range(num_terminals):
                task = self.test_single_terminal(i + 1, test_duration)
                tasks.append(task)

            # 并发执行所有测试
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return results

        # 运行测试
        try:
            results = asyncio.run(run_all_tests())
        except Exception as e:
            print(f"测试执行失败: {e}")
            return None

        total_time = time.time() - start_time

        # 生成测试报告
        self.generate_report(num_terminals, test_duration, total_time)

        return results

    def generate_report(self, num_terminals, test_duration, total_time):
        """生成测试报告"""
        if not self.results:
            print("没有测试结果可生成报告")
            return

        print("\n" + "=" * 60)
        print("性能测试报告")
        print("=" * 60)

        # 总体统计
        successful_terminals = len(
            [r for r in self.results if r.get("message_count", 0) > 0]
        )
        total_messages = sum(r.get("message_count", 0) for r in self.results)
        total_errors = sum(r.get("error_count", 0) for r in self.results)

        print(f"测试配置:")
        print(f"  - 终端数量: {num_terminals}")
        print(f"  - 测试时长: {test_duration}秒")
        print(f"  - 总耗时: {total_time:.2f}秒")
        print()

        print(f"测试结果汇总:")
        print(f"  - 成功连接的终端: {successful_terminals}/{num_terminals}")
        print(f"  - 总消息数: {total_messages}")
        print(f"  - 总错误数: {total_errors}")
        print(f"  - 整体成功率: {(successful_terminals/num_terminals*100):.1f}%")
        print()

        # 响应时间统计
        all_response_times = []
        for result in self.results:
            if result.get("avg_response_time", 0) > 0:
                all_response_times.append(result["avg_response_time"])

        if all_response_times:
            print(f"响应时间统计:")
            print(f"  - 平均响应时间: {statistics.mean(all_response_times):.3f}秒")
            print(f"  - 最大响应时间: {max(all_response_times):.3f}秒")
            print(f"  - 最小响应时间: {min(all_response_times):.3f}秒")
            if len(all_response_times) > 1:
                print(
                    f"  - 响应时间标准差: {statistics.stdev(all_response_times):.3f}秒"
                )
            print()

        # 吞吐量统计
        all_throughputs = [
            r.get("messages_per_second", 0)
            for r in self.results
            if r.get("messages_per_second", 0) > 0
        ]
        if all_throughputs:
            print(f"吞吐量统计:")
            print(
                f"  - 平均吞吐量: {statistics.mean(all_throughputs):.2f} 消息/秒/终端"
            )
            print(f"  - 总吞吐量: {sum(all_throughputs):.2f} 消息/秒")
            print()

        # 性能分析
        print("性能分析:")
        if successful_terminals < num_terminals:
            print("  ⚠️  部分终端连接失败，可能存在连接数限制或服务器资源不足")

        if all_response_times and statistics.mean(all_response_times) > 1.0:
            print("  ⚠️  平均响应时间超过1秒，可能存在性能瓶颈")

        if total_errors > total_messages * 0.1:
            print("  ⚠️  错误率超过10%，网络或服务器稳定性可能存在问题")

        if successful_terminals == num_terminals and all_response_times:
            if (
                statistics.mean(all_response_times) < 0.5
                and total_errors < total_messages * 0.05
            ):
                print("  ✅ 整体性能表现良好")

        print("\n" + "=" * 60)

        # 保存详细结果到文件
        self.save_results_to_file(num_terminals, test_duration)

    def save_results_to_file(self, num_terminals, test_duration):
        """保存详细结果到文件"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"performance_test_{num_terminals}terminals_{test_duration}s_{timestamp}.json"

        report_data = {
            "timestamp": timestamp,
            "test_config": {
                "num_terminals": num_terminals,
                "test_duration": test_duration,
                "base_url": self.base_url,
                "namespace": self.namespace,
                "pod_name": self.pod_name,
            },
            "results": self.results,
            "summary": {
                "successful_terminals": len(
                    [r for r in self.results if r.get("message_count", 0) > 0]
                ),
                "total_messages": sum(r.get("message_count", 0) for r in self.results),
                "total_errors": sum(r.get("error_count", 0) for r in self.results),
                "avg_response_time": (
                    statistics.mean(
                        [
                            r.get("avg_response_time", 0)
                            for r in self.results
                            if r.get("avg_response_time", 0) > 0
                        ]
                    )
                    if any(r.get("avg_response_time", 0) > 0 for r in self.results)
                    else 0
                ),
            },
        }

        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(report_data, f, indent=2, ensure_ascii=False)
            print(f"详细测试结果已保存到: {filename}")
        except Exception as e:
            print(f"保存结果文件失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="Kubernetes Web终端性能测试工具")
    parser.add_argument(
        "--url", required=True, help="WebSocket服务器URL (例如: ws://localhost:8080)"
    )
    parser.add_argument("--namespace", default="default", help="Kubernetes命名空间")
    parser.add_argument("--pod", default="test-pod", help="Pod名称")
    parser.add_argument("--terminals", type=int, default=5, help="并发终端数量")
    parser.add_argument("--duration", type=int, default=30, help="测试时长(秒)")
    parser.add_argument(
        "--test-cases", nargs="+", type=int, help="测试不同终端数量的性能对比"
    )

    args = parser.parse_args()

    tester = TerminalPerformanceTester(args.url, args.namespace, args.pod)

    if args.test_cases:
        # 运行多个测试用例
        for num_terminals in args.test_cases:
            print(f"\n{'='*60}")
            print(f"测试用例: {num_terminals} 个终端")
            print("=" * 60)
            tester.run_concurrent_test(num_terminals, args.duration)
            time.sleep(5)  # 测试间隔
    else:
        # 运行单个测试
        tester.run_concurrent_test(args.terminals, args.duration)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # 如果没有参数，使用默认配置
        print("使用默认配置进行测试...")
        tester = TerminalPerformanceTester("ws://localhost:8080", "default", "test-pod")
        tester.run_concurrent_test(5, 30)
    else:
        main()
