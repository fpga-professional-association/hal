// Tiny LUT + FF + carry-chain design for HAL fixture generation.
// 8-bit accumulating counter: count <= count + addend when enabled.
module counter_adder (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire [7:0] addend,
    output reg  [7:0] count,
    output wire       carry_out
);

    wire [8:0] sum = {1'b0, count} + {1'b0, addend};

    assign carry_out = sum[8];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count <= 8'h00;
        end else if (en) begin
            count <= sum[7:0];
        end
    end

endmodule
