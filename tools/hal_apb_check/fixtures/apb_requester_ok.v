// APB4 requester that holds address and control across wait states -- correct.
//
// Gate-level Verilog against EXAMPLE_GATE_LIBRARY. FFR is Q' = R ? 0 : (D & CE),
// so CE is tied to VCC and PRESETn is inverted into the active-high reset R.
// MUX is O = (~S & I0) | (S & I1).
//
// State machine, one step per rising PCLK edge:
//   sel_q'  = (sel_q & ~(en_q & PREADY)) | (~sel_q & START)
//   en_q'   = (sel_q & ~en_q) | (en_q & ~PREADY)
//   addr_q' = sel_q ? addr_q : ADDR_IN      (captured in IDLE, held while selected)
//   wr_q'   = sel_q ? wr_q   : WR_IN
//   wdat_q' = sel_q ? wdat_q : WDATA_IN
//
//   PSEL = sel_q   PENABLE = en_q   PADDR_0 = addr_q
//   PWRITE = wr_q  PWDATA_0 = wdat_q
//
// PREADY is a primary input, deliberately unconstrained: the checker must be
// free to insert arbitrarily long wait states, which is where a stability bug
// would show.

module apb_requester_ok (
    PCLK,
    PRESETn,
    START,
    ADDR_IN,
    WR_IN,
    WDATA_IN,
    PREADY,
    PSEL,
    PENABLE,
    PADDR_0,
    PWRITE,
    PWDATA_0
);
    input PCLK;
    input PRESETn;
    input START;
    input ADDR_IN;
    input WR_IN;
    input WDATA_IN;
    input PREADY;
    output PSEL;
    output PENABLE;
    output PADDR_0;
    output PWRITE;
    output PWDATA_0;

    wire PCLK;
    wire PRESETn;
    wire START;
    wire ADDR_IN;
    wire WR_IN;
    wire WDATA_IN;
    wire PREADY;
    wire PSEL;
    wire PENABLE;
    wire PADDR_0;
    wire PWRITE;
    wire PWDATA_0;

    wire vdd;
    wire rst;
    wire sel_q;
    wire en_q;
    wire addr_q;
    wire wr_q;
    wire wdat_q;
    wire done;
    wire ndone;
    wire sel_hold;
    wire nsel;
    wire start_sel;
    wire sel_d;
    wire nen;
    wire setup;
    wire npready;
    wire waiting;
    wire en_d;
    wire addr_d;
    wire wr_d;
    wire wdat_d;

    VCC vcc_inst (
        .O(vdd)
    );
    INV rst_inv (
        .I(PRESETn),
        .O(rst)
    );

    BUF psel_buf (
        .I(sel_q),
        .O(PSEL)
    );
    BUF penable_buf (
        .I(en_q),
        .O(PENABLE)
    );
    BUF paddr_buf (
        .I(addr_q),
        .O(PADDR_0)
    );
    BUF pwrite_buf (
        .I(wr_q),
        .O(PWRITE)
    );
    BUF pwdata_buf (
        .I(wdat_q),
        .O(PWDATA_0)
    );

    AND2 done_and (
        .I0(en_q),
        .I1(PREADY),
        .O(done)
    );
    INV done_inv (
        .I(done),
        .O(ndone)
    );
    AND2 sel_hold_and (
        .I0(sel_q),
        .I1(ndone),
        .O(sel_hold)
    );
    INV sel_inv (
        .I(sel_q),
        .O(nsel)
    );
    AND2 start_and (
        .I0(nsel),
        .I1(START),
        .O(start_sel)
    );
    OR2 sel_or (
        .I0(sel_hold),
        .I1(start_sel),
        .O(sel_d)
    );

    INV en_inv (
        .I(en_q),
        .O(nen)
    );
    AND2 setup_and (
        .I0(sel_q),
        .I1(nen),
        .O(setup)
    );
    INV pready_inv (
        .I(PREADY),
        .O(npready)
    );
    AND2 wait_and (
        .I0(en_q),
        .I1(npready),
        .O(waiting)
    );
    OR2 en_or (
        .I0(setup),
        .I1(waiting),
        .O(en_d)
    );

    MUX addr_mux (
        .I0(ADDR_IN),
        .I1(addr_q),
        .S(sel_q),
        .O(addr_d)
    );
    MUX wr_mux (
        .I0(WR_IN),
        .I1(wr_q),
        .S(sel_q),
        .O(wr_d)
    );
    MUX wdat_mux (
        .I0(WDATA_IN),
        .I1(wdat_q),
        .S(sel_q),
        .O(wdat_d)
    );

    FFR sel_reg (
        .C(PCLK),
        .CE(vdd),
        .D(sel_d),
        .R(rst),
        .Q(sel_q)
    );
    FFR en_reg (
        .C(PCLK),
        .CE(vdd),
        .D(en_d),
        .R(rst),
        .Q(en_q)
    );
    FFR addr_reg (
        .C(PCLK),
        .CE(vdd),
        .D(addr_d),
        .R(rst),
        .Q(addr_q)
    );
    FFR wr_reg (
        .C(PCLK),
        .CE(vdd),
        .D(wr_d),
        .R(rst),
        .Q(wr_q)
    );
    FFR wdat_reg (
        .C(PCLK),
        .CE(vdd),
        .D(wdat_d),
        .R(rst),
        .Q(wdat_q)
    );
endmodule
